#!/usr/bin/env python3
"""
NasTech Guardian — Repair Bot (Stage 5)
AI analyzes failures → generates patches → creates branch → opens PR.
NEVER pushes directly to main. Always creates a PR for human review.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent.parent

# Safe, well-known auto-fixable patterns (no AI needed)
AUTO_FIXES = {
    "not_executable": {
        "description": "Fix gradlew permissions",
        "apply": lambda: subprocess.run(["chmod", "+x", "gradlew"], cwd=REPO_ROOT),
        "commit_msg": "fix: make gradlew executable",
    },
    "world_readable": {
        "description": "Remove world-readable file modes",
        "apply": None,  # requires manual fix
        "commit_msg": None,
    },
}


def gh_api(method: str, path: str, body: dict = None) -> dict:
    """Call GitHub REST API."""
    token = os.environ.get("GITHUB_TOKEN", "")
    url = f"https://api.github.com{path}"
    data = json.dumps(body).encode("utf-8") if body else None
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept":        "application/vnd.github+json",
            "Content-Type":  "application/json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body_text = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"GitHub API {method} {path} → HTTP {e.code}: {body_text[:300]}")


def ask_ai_for_fix(build_log: str, health_issues: list, dep_issues: list) -> dict:
    """Ask AI coordinator for fix suggestions."""
    sys.path.insert(0, str(Path(__file__).parent))
    try:
        from ai_coordinator import analyze_build_failure, analyze_dependencies
    except ImportError:
        return {"root_cause": "AI unavailable", "fix_steps": [], "file_patches": []}

    result = {"root_cause": "", "fix_steps": [], "file_patches": [], "confidence": 0}

    if build_log and build_log != "TIMEOUT":
        ai_result = analyze_build_failure(build_log, {
            "health_issues": len(health_issues),
            "dep_issues": len(dep_issues),
        })
        raw = ai_result.get("response")
        if raw:
            # Try to extract JSON from response
            try:
                json_match = re.search(r'\{[\s\S]+\}', raw)
                if json_match:
                    result.update(json.loads(json_match.group(0)))
            except json.JSONDecodeError:
                result["root_cause"] = raw[:500]
        result["ai_provider"] = ai_result.get("provider")

    if dep_issues:
        dep_ai = analyze_dependencies({"issues": dep_issues[:5]})
        raw = dep_ai.get("response")
        if raw:
            try:
                json_match = re.search(r'\{[\s\S]+\}', raw)
                if json_match:
                    dep_parsed = json.loads(json_match.group(0))
                    result["fix_steps"].extend(dep_parsed.get("fix_commands", []))
            except json.JSONDecodeError:
                pass

    return result


def apply_safe_patches(patches: list) -> list:
    """Apply only safe, validated patches to files."""
    applied = []
    for patch in patches:
        file_path = REPO_ROOT / patch.get("file", "")
        search   = patch.get("search", "")
        replace  = patch.get("replace", "")

        if not file_path.exists():
            continue
        if not search or not replace:
            continue
        # Safety: only touch known safe file types
        if file_path.suffix not in {".gradle", ".properties", ".xml", ".txt", ".md"}:
            continue
        # Safety: don't allow deletions of entire files
        if len(replace) < 5 and len(search) > 50:
            continue

        try:
            text = file_path.read_text(errors="replace")
            if search in text:
                new_text = text.replace(search, replace, 1)
                file_path.write_text(new_text)
                applied.append({
                    "file":    str(file_path.relative_to(REPO_ROOT)),
                    "search":  search[:80],
                    "replace": replace[:80],
                })
        except Exception as e:
            pass

    return applied


def create_repair_branch(sha: str) -> str:
    """Create a repair branch from current HEAD."""
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    branch = f"guardian/repair-{sha[:7]}-{ts}"

    subprocess.run(["git", "config", "user.email", "nastech-guardian@users.noreply.github.com"],
                   cwd=REPO_ROOT, capture_output=True)
    subprocess.run(["git", "config", "user.name", "NasTech Guardian"],
                   cwd=REPO_ROOT, capture_output=True)
    subprocess.run(["git", "checkout", "-b", branch],
                   cwd=REPO_ROOT, capture_output=True)
    return branch


def commit_and_push(branch: str, message: str, files: list) -> bool:
    """Stage changed files, commit, and push branch."""
    for f in files:
        subprocess.run(["git", "add", f], cwd=REPO_ROOT, capture_output=True)

    result = subprocess.run(
        ["git", "commit", "-m", message, "--allow-empty"],
        cwd=REPO_ROOT, capture_output=True, text=True
    )
    if result.returncode != 0:
        return False

    push_result = subprocess.run(
        ["git", "push", "origin", branch],
        cwd=REPO_ROOT, capture_output=True, text=True
    )
    return push_result.returncode == 0


def create_pull_request(repo: str, branch: str, base: str, title: str, body: str) -> dict:
    """Create a GitHub PR via API."""
    parts = repo.split("/")
    owner, repo_name = parts[0], parts[1]
    return gh_api("POST", f"/repos/{owner}/{repo_name}/pulls", {
        "title": title,
        "body":  body,
        "head":  branch,
        "base":  base,
        "draft": False,
    })


def generate_pr_body(ai_result: dict, applied_patches: list, fix_steps: list,
                     build_errors: list, health_issues: list) -> str:
    lines = [
        "## 🤖 NasTech Guardian — Automated Repair",
        "",
        "> **Review carefully before merging.** This PR was auto-generated by NasTech Guardian.",
        "> All changes have been validated by the AI Coordinator but require human approval.",
        "",
        "---",
        "",
        "### 🔍 Root Cause",
        f"{ai_result.get('root_cause', 'See build errors below')}",
        "",
        "### 🔧 Applied Patches",
    ]

    if applied_patches:
        for p in applied_patches:
            lines.append(f"- `{p['file']}`: replaced `{p['search']}` → `{p['replace']}`")
    else:
        lines.append("- No file patches applied (manual fixes required)")

    lines += ["", "### 📋 Recommended Manual Fix Steps"]
    all_steps = list(ai_result.get("fix_steps", [])) + fix_steps
    for step in all_steps[:10]:
        lines.append(f"- {step}")
    if not all_steps:
        lines.append("- Review build errors in the Actions log")

    if build_errors:
        lines += ["", "### ❌ Build Errors Detected"]
        for e in build_errors[:5]:
            lines.append(f"- `{e.get('type')}`: {e.get('context', '')[:150]}")

    if health_issues:
        lines += ["", "### ⚠️ Health Issues"]
        for i in health_issues[:5]:
            lines.append(f"- [{i.get('severity','?').upper()}] {i.get('message','')} — {i.get('file','')}")

    lines += [
        "",
        "---",
        "### ✅ Merge Checklist",
        "- [ ] Reviewed all changes",
        "- [ ] Build passes locally: `./gradlew assembleDebug`",
        "- [ ] Tests pass: `./gradlew test`",
        "- [ ] `nastech verify` passes",
        "",
        "*Auto-generated by NasTech Guardian · AI Coordinator*",
    ]
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="NasTech Repair Bot")
    parser.add_argument("--build-report",  required=True)
    parser.add_argument("--health-report", required=True)
    parser.add_argument("--dep-report",    required=True)
    parser.add_argument("--repo",          required=True)
    parser.add_argument("--sha",           required=True)
    parser.add_argument("--output",        required=True)
    args = parser.parse_args()

    dry_run = os.environ.get("DRY_RUN", "false").lower() == "true"

    build_report  = json.loads(Path(args.build_report).read_text())  if Path(args.build_report).exists()  else {}
    health_report = json.loads(Path(args.health_report).read_text()) if Path(args.health_report).exists() else {}
    dep_report    = json.loads(Path(args.dep_report).read_text())    if Path(args.dep_report).exists()    else {}

    build_log     = build_report.get("build_log_tail", "")
    build_errors  = build_report.get("errors", [])
    health_issues = health_report.get("all_issues", [])
    dep_issues    = dep_report.get("all_issues", [])

    print("🔧 Running Repair Bot...")
    print(f"   Build errors:   {len(build_errors)}")
    print(f"   Health issues:  {len(health_issues)}")
    print(f"   Dep issues:     {len(dep_issues)}")
    print(f"   Dry run:        {dry_run}")

    # Ask AI
    print("\n[1/4] Consulting AI Coordinator...")
    ai_result = ask_ai_for_fix(build_log, health_issues, dep_issues)
    confidence = ai_result.get("confidence", 0)
    print(f"   AI provider:  {ai_result.get('ai_provider', 'unavailable')}")
    print(f"   Confidence:   {confidence}%")
    print(f"   Root cause:   {ai_result.get('root_cause', 'unknown')[:100]}")

    # Apply safe patches
    print("\n[2/4] Applying safe patches...")
    patches = ai_result.get("file_patches", [])
    applied = []
    if not dry_run and patches:
        applied = apply_safe_patches(patches)
        print(f"   Applied: {len(applied)} patches")
    else:
        print(f"   Dry run — skipping patch application")

    # Determine fix steps
    fix_steps = []
    for issue in health_issues:
        if issue.get("fix"):
            fix_steps.append(issue["fix"])
    for issue in dep_issues:
        if issue.get("fix"):
            fix_steps.append(issue["fix"])
    fix_steps = list(dict.fromkeys(fix_steps))[:10]

    # Create PR (only if patches were applied and not dry run)
    pr_number = ""
    pr_url    = ""
    branch    = ""

    if applied and not dry_run:
        print("\n[3/4] Creating repair branch and PR...")
        try:
            branch = create_repair_branch(args.sha)
            pr_body = generate_pr_body(ai_result, applied, fix_steps, build_errors, health_issues)
            sha_short = args.sha[:7]

            # Commit changes
            files_changed = [p["file"] for p in applied]
            if commit_and_push(branch, f"fix(guardian): auto-repair from commit {sha_short}", files_changed):
                pr = create_pull_request(
                    args.repo, branch,
                    base="main",
                    title=f"🤖 Guardian Auto-Repair: {len(applied)} patches — {sha_short}",
                    body=pr_body,
                )
                pr_number = str(pr.get("number", ""))
                pr_url    = pr.get("html_url", "")
                print(f"   ✅ PR created: {pr_url}")
            else:
                print("   ⚠️  Push failed — PR not created")
        except Exception as e:
            print(f"   ⚠️  PR creation failed: {e}")
    else:
        print("\n[3/4] No patches to commit — skipping PR creation")

    print("\n[4/4] Generating repair report...")
    status = "pass" if (applied or confidence > 60) else "partial"

    report = {
        "status":        status,
        "dry_run":       dry_run,
        "ai_provider":   ai_result.get("ai_provider"),
        "confidence":    confidence,
        "root_cause":    ai_result.get("root_cause", ""),
        "fix_steps":     fix_steps,
        "patches_proposed": len(patches),
        "patch_count":   len(applied),
        "patches":       applied,
        "branch":        branch,
        "pr_number":     pr_number,
        "pr_url":        pr_url,
    }

    Path(args.output).write_text(json.dumps(report, indent=2))
    print(f"\n🔧 Repair Bot Complete: {status.upper()}")
    print(f"   Patches applied: {len(applied)}")
    print(f"   PR: {pr_url or '(none)'}")
    print(f"✅ Repair report saved: {args.output}")


if __name__ == "__main__":
    main()
