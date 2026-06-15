#!/usr/bin/env python3
"""
NasTech Guardian — Identity Bot (Stage 1)
Detects: language stack, build system, package manager, CI system.
Generates the repository profile JSON used by all downstream agents.
"""

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from datetime import datetime, timezone


REPO_ROOT = Path(__file__).parent.parent.parent


def detect_language_stack() -> list:
    """Detect all language stacks present in the repo."""
    stacks = []

    indicators = {
        "java":    ["**/*.java"],
        "kotlin":  ["**/*.kt", "**/*.kts"],
        "python":  ["**/*.py", "requirements.txt", "setup.py", "pyproject.toml"],
        "node":    ["package.json", "yarn.lock", "package-lock.json"],
        "rust":    ["Cargo.toml", "Cargo.lock"],
        "go":      ["go.mod", "go.sum"],
        "cpp":     ["**/*.cpp", "**/*.c", "Android.mk", "CMakeLists.txt"],
        "shell":   ["**/*.sh"],
    }

    for lang, patterns in indicators.items():
        for pat in patterns:
            if "*" in pat:
                found = list(REPO_ROOT.rglob(pat.lstrip("**/").lstrip("*")))
                found = [f for f in found if ".git" not in str(f) and "build" not in str(f)]
                if found:
                    stacks.append(lang)
                    break
            else:
                if (REPO_ROOT / pat).exists():
                    stacks.append(lang)
                    break

    return list(dict.fromkeys(stacks))  # deduplicate, preserve order


def detect_build_system() -> str:
    """Detect primary build system."""
    checks = [
        ("gradle",  ["build.gradle", "gradlew"]),
        ("maven",   ["pom.xml"]),
        ("cmake",   ["CMakeLists.txt"]),
        ("make",    ["Makefile", "GNUmakefile"]),
        ("poetry",  ["pyproject.toml"]),
        ("npm",     ["package.json"]),
        ("cargo",   ["Cargo.toml"]),
        ("go",      ["go.mod"]),
    ]
    for system, files in checks:
        if any((REPO_ROOT / f).exists() for f in files):
            return system
    return "unknown"


def detect_package_manager() -> list:
    """Detect all active package managers."""
    managers = []
    checks = {
        "gradle":  ["build.gradle", "gradlew"],
        "pip":     ["requirements.txt", "setup.py", "pyproject.toml"],
        "npm":     ["package.json"],
        "yarn":    ["yarn.lock"],
        "cargo":   ["Cargo.toml"],
        "go":      ["go.mod"],
    }
    for pm, files in checks.items():
        if any((REPO_ROOT / f).exists() for f in files):
            managers.append(pm)
    return managers


def detect_ci_system() -> list:
    """Detect CI systems configured."""
    ci = []
    if (REPO_ROOT / ".github" / "workflows").exists():
        workflows = list((REPO_ROOT / ".github" / "workflows").glob("*.yml"))
        if workflows:
            ci.append("github-actions")
    if (REPO_ROOT / ".circleci").exists():
        ci.append("circleci")
    if (REPO_ROOT / ".travis.yml").exists():
        ci.append("travis-ci")
    if (REPO_ROOT / "Jenkinsfile").exists():
        ci.append("jenkins")
    return ci or ["none"]


def detect_android_info() -> dict:
    """Read Android-specific metadata from gradle.properties."""
    info = {}
    props_path = REPO_ROOT / "gradle.properties"
    if props_path.exists():
        text = props_path.read_text(errors="replace")
        for key in ["minSdkVersion", "targetSdkVersion", "compileSdkVersion", "ndkVersion"]:
            m = re.search(rf"^{key}\s*=\s*(.+)$", text, re.MULTILINE)
            if m:
                info[key] = m.group(1).strip()

    build_gradle = REPO_ROOT / "app" / "build.gradle"
    if build_gradle.exists():
        text = build_gradle.read_text(errors="replace")
        m = re.search(r'versionName\s+"([^"]+)"', text)
        if m:
            info["versionName"] = m.group(1)
        m = re.search(r'versionCode\s+(\d+)', text)
        if m:
            info["versionCode"] = m.group(1)

    return info


def detect_modules() -> list:
    """Detect Gradle submodules from settings.gradle."""
    settings = REPO_ROOT / "settings.gradle"
    if not settings.exists():
        return []
    text = settings.read_text(errors="replace")
    return re.findall(r"include\s+['\"]:([\w\-]+)['\"]", text)


def detect_workflows() -> list:
    """List all GitHub Action workflows."""
    wf_dir = REPO_ROOT / ".github" / "workflows"
    if not wf_dir.exists():
        return []
    return [f.name for f in wf_dir.glob("*.yml")]


def detect_priority(android_info: dict, language_stack: list) -> str:
    """Determine repository priority level."""
    ver = android_info.get("versionName", "")
    if ver.startswith("0."):
        return "high"
    if "java" in language_stack or "kotlin" in language_stack:
        return "critical"
    return "normal"


def count_source_files() -> dict:
    """Count source files by language."""
    counts = {}
    exts = {
        ".java": "java", ".kt": "kotlin", ".py": "python",
        ".js": "javascript", ".ts": "typescript", ".rs": "rust",
        ".go": "go", ".cpp": "cpp", ".c": "c", ".sh": "shell",
    }
    for path in REPO_ROOT.rglob("*"):
        if ".git" in str(path) or "build" in str(path):
            continue
        lang = exts.get(path.suffix)
        if lang:
            counts[lang] = counts.get(lang, 0) + 1
    return counts


def get_recent_commits(n: int = 10) -> list:
    """Get recent commit messages."""
    try:
        result = subprocess.run(
            ["git", "log", f"-{n}", "--format=%h %s"],
            capture_output=True, text=True, cwd=REPO_ROOT, timeout=10
        )
        return result.stdout.strip().split("\n") if result.stdout.strip() else []
    except Exception:
        return []


def generate_profile(owner: str, repo: str, sha: str) -> dict:
    """Generate the complete repository identity profile."""
    langs = detect_language_stack()
    build_sys = detect_build_system()
    pkg_mgrs = detect_package_manager()
    ci_sys = detect_ci_system()
    android = detect_android_info()
    modules = detect_modules()
    workflows = detect_workflows()
    src_counts = count_source_files()
    commits = get_recent_commits()
    priority = detect_priority(android, langs)

    primary_lang = langs[0] if langs else "unknown"

    profile = {
        "organization":    owner,
        "repository":      repo,
        "sha":             sha,
        "sha_short":       sha[:7] if len(sha) >= 7 else sha,
        "type":            primary_lang,
        "language_stack":  ",".join(langs),
        "build_system":    build_sys,
        "package_manager": ",".join(pkg_mgrs),
        "ci_system":       ",".join(ci_sys),
        "priority":        priority,
        "guardian_enabled": True,
        "android": android,
        "modules": modules,
        "workflows": workflows,
        "source_file_counts": src_counts,
        "recent_commits": commits,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "termux_project": (
            "com.termux" in (REPO_ROOT / "app" / "build.gradle").read_text(errors="replace")
            if (REPO_ROOT / "app" / "build.gradle").exists()
            else False
        ),
        "nastech_ai": (
            "NasTech" in (REPO_ROOT / "app" / "build.gradle").read_text(errors="replace")
            if (REPO_ROOT / "app" / "build.gradle").exists()
            else False
        ),
    }

    # Print human-readable summary
    print(f"\n🔍 Repository Identity Profile")
    print(f"   Organization:    {owner}")
    print(f"   Repository:      {repo}")
    print(f"   Languages:       {', '.join(langs)}")
    print(f"   Build System:    {build_sys}")
    print(f"   Package Mgrs:    {', '.join(pkg_mgrs)}")
    print(f"   CI System:       {', '.join(ci_sys)}")
    print(f"   Priority:        {priority}")
    print(f"   Modules:         {', '.join(modules)}")
    print(f"   Android SDK:     {android.get('compileSdkVersion','?')}")
    print(f"   Version:         {android.get('versionName','?')}")
    print(f"   Termux Project:  {profile['termux_project']}")
    print(f"   NasTech AI:      {profile['nastech_ai']}")
    print()

    return profile


def main():
    parser = argparse.ArgumentParser(description="NasTech Identity Bot")
    parser.add_argument("--owner",  required=True)
    parser.add_argument("--repo",   required=True)
    parser.add_argument("--sha",    required=True)
    parser.add_argument("--output", default="identity_profile.json")
    args = parser.parse_args()

    profile = generate_profile(args.owner, args.repo, args.sha)
    Path(args.output).write_text(json.dumps(profile, indent=2))
    print(f"✅ Identity profile saved: {args.output}")


if __name__ == "__main__":
    main()
