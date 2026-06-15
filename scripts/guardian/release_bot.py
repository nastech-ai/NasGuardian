#!/usr/bin/env python3
"""
NasTech Guardian — Release Bot (Stage 6)
Generates changelog · Packages artifacts · Creates GitHub release.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent.parent


def gh_api(method: str, path: str, body: dict = None) -> dict:
    token = os.environ.get("GITHUB_TOKEN", "")
    url   = f"https://api.github.com{path}"
    data  = json.dumps(body).encode("utf-8") if body else None
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={
            "Authorization":        f"Bearer {token}",
            "Accept":               "application/vnd.github+json",
            "Content-Type":         "application/json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"GitHub API {method} {path} → HTTP {e.code}: {e.read().decode()[:300]}")


def get_commits_since_last_tag(repo: str) -> list:
    """Get commit messages since last tag for changelog."""
    try:
        last_tag = subprocess.run(
            ["git", "describe", "--tags", "--abbrev=0"],
            capture_output=True, text=True, cwd=REPO_ROOT, timeout=15
        ).stdout.strip()

        if last_tag:
            range_spec = f"{last_tag}..HEAD"
        else:
            range_spec = "HEAD~20..HEAD"

        result = subprocess.run(
            ["git", "log", range_spec, "--format=%h|||%s|||%an"],
            capture_output=True, text=True, cwd=REPO_ROOT, timeout=15
        )
        commits = []
        for line in result.stdout.strip().split("\n"):
            if "|||" in line:
                parts = line.split("|||")
                if len(parts) == 3:
                    commits.append({
                        "sha":     parts[0].strip(),
                        "message": parts[1].strip(),
                        "author":  parts[2].strip(),
                    })
        return commits
    except Exception:
        return []


def categorize_commits(commits: list) -> dict:
    """Categorize commits by conventional commit type."""
    categories = {
        "features":     [],
        "fixes":        [],
        "dependencies": [],
        "ci":           [],
        "docs":         [],
        "other":        [],
    }
    patterns = {
        "features":     re.compile(r'^(feat|feature|add|new)', re.IGNORECASE),
        "fixes":        re.compile(r'^(fix|bug|patch|hotfix)', re.IGNORECASE),
        "dependencies": re.compile(r'^(build|dep|deps|upgrade|update|bump)', re.IGNORECASE),
        "ci":           re.compile(r'^(ci|workflow|action|cd)', re.IGNORECASE),
        "docs":         re.compile(r'^(docs|doc|readme|comment)', re.IGNORECASE),
    }
    for commit in commits:
        msg = commit["message"]
        placed = False
        for cat, pat in patterns.items():
            if pat.match(msg):
                categories[cat].append(commit)
                placed = True
                break
        if not placed:
            categories["other"].append(commit)
    return categories


def generate_changelog(commits: list, version: str, repo: str) -> str:
    """Generate markdown changelog from commits."""
    # Try AI-enhanced changelog first
    sys.path.insert(0, str(Path(__file__).parent))
    ai_changelog = None
    try:
        from ai_coordinator import generate_changelog as ai_gen
        msgs = [c["message"] for c in commits[:30]]
        ai_result = ai_gen(msgs, version)
        if ai_result and len(ai_result) > 100:
            ai_changelog = ai_result
    except Exception:
        pass

    if ai_changelog:
        return ai_changelog

    # Fallback: structured categorized changelog
    categories = categorize_commits(commits)
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    lines = [
        f"## NasTech AI Terminal {version}",
        f"*Released: {date_str}*",
        "",
    ]

    if categories["features"]:
        lines.append("### ✨ Features")
        for c in categories["features"]:
            lines.append(f"- {c['message']} (`{c['sha']}`)")
        lines.append("")

    if categories["fixes"]:
        lines.append("### 🐛 Bug Fixes")
        for c in categories["fixes"]:
            lines.append(f"- {c['message']} (`{c['sha']}`)")
        lines.append("")

    if categories["dependencies"]:
        lines.append("### 📦 Dependencies")
        for c in categories["dependencies"]:
            lines.append(f"- {c['message']} (`{c['sha']}`)")
        lines.append("")

    if categories["ci"]:
        lines.append("### ⚙️ CI / Workflows")
        for c in categories["ci"]:
            lines.append(f"- {c['message']} (`{c['sha']}`)")
        lines.append("")

    other = categories["other"] + categories["docs"]
    if other:
        lines.append("### 🔧 Other Changes")
        for c in other[:10]:
            lines.append(f"- {c['message']} (`{c['sha']}`)")
        lines.append("")

    lines += [
        "---",
        "### 📱 NasTech AI Terminal Features",
        "- 🤖 AI brain with Groq / Gemini / OpenRouter streaming",
        "- `$` command system (ubuntu, install, ai, speak, git, system)",
        "- 🎤 Piper TTS (`$ speak [text]`)",
        "- 🌐 Built-in browser",
        "- 🔒 Biometric lock screen",
        "- 📁 VS Code sidebar",
        "- 🐧 Ubuntu proot layer",
        "- 🛡️ NasTech Guardian CI/CD",
        "",
        "### 📥 Downloads",
        "- `*-apt-android-7*` — Android 7.0+ (recommended)",
        "- `*-apt-android-5*` — Android 5.0+ (older devices)",
    ]
    return "\n".join(lines)


def get_app_version() -> str:
    build_gradle = REPO_ROOT / "app" / "build.gradle"
    if not build_gradle.exists():
        return "0.0.0"
    text = build_gradle.read_text(errors="replace")
    m = re.search(r'versionName\s+"([^"]+)"', text)
    return m.group(1) if m else "0.0.0"


def release_exists(repo: str, tag: str) -> bool:
    try:
        owner, repo_name = repo.split("/")
        gh_api("GET", f"/repos/{owner}/{repo_name}/releases/tags/{tag}")
        return True
    except Exception:
        return False


def create_or_update_release(repo: str, tag: str, name: str, body: str, sha: str) -> dict:
    owner, repo_name = repo.split("/")
    if release_exists(repo, tag):
        # Update existing release body
        releases = gh_api("GET", f"/repos/{owner}/{repo_name}/releases/tags/{tag}")
        release_id = releases.get("id")
        if release_id:
            return gh_api("PATCH", f"/repos/{owner}/{repo_name}/releases/{release_id}", {
                "name":  name,
                "body":  body,
                "draft": False,
            })
    else:
        return gh_api("POST", f"/repos/{owner}/{repo_name}/releases", {
            "tag_name":         tag,
            "target_commitish": sha,
            "name":             name,
            "body":             body,
            "draft":            False,
            "prerelease":       False,
        })
    return {}


def main():
    parser = argparse.ArgumentParser(description="NasTech Release Bot")
    parser.add_argument("--repo",   required=True)
    parser.add_argument("--sha",    required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    print("🚀 Running Release Bot...")

    version = get_app_version()
    commits = get_commits_since_last_tag(args.repo)
    print(f"   Version:  {version}")
    print(f"   Commits:  {len(commits)} since last tag")

    print("\n[1/3] Generating changelog...")
    changelog = generate_changelog(commits, version, args.repo)
    changelog_lines = len(changelog.split("\n"))
    print(f"   Generated {changelog_lines} lines")

    print("\n[2/3] Creating GitHub release...")
    tag = "nastech-v6.0"
    release_name = f"NasTech AI Terminal v{version}"
    release_url = ""
    status = "pass"

    try:
        release = create_or_update_release(
            args.repo, tag, release_name,
            body=changelog, sha=args.sha
        )
        release_url = release.get("html_url", "")
        print(f"   ✅ Release: {release_url}")
    except Exception as e:
        print(f"   ⚠️  Release creation failed: {e}")
        status = "warn"

    # Save changelog
    (REPO_ROOT / "CHANGELOG.md").write_text(changelog)
    print("\n[3/3] Changelog saved to CHANGELOG.md")

    report = {
        "status":          status,
        "version":         version,
        "release_tag":     tag,
        "release_name":    release_name,
        "release_url":     release_url,
        "changelog_lines": changelog_lines,
        "commit_count":    len(commits),
    }

    Path(args.output).write_text(json.dumps(report, indent=2))
    print(f"\n🚀 Release Bot Complete: {status.upper()}")
    print(f"✅ Release report saved: {args.output}")


if __name__ == "__main__":
    main()
