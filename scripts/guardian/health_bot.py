#!/usr/bin/env python3
"""
NasTech Guardian — Health Bot (Stage 3)
Source code scan · Configuration scan · Environment scan · Secret scan · Build readiness
"""

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import List, Dict

REPO_ROOT = Path(__file__).parent.parent.parent

SECRET_PATTERNS = [
    (re.compile(r'(?i)(api[_\-]?key|apikey)\s*[=:]\s*["\']([A-Za-z0-9_\-]{16,})["\']'), "api_key"),
    (re.compile(r'(?i)(secret|token|password|passwd|pwd)\s*[=:]\s*["\']([^"\']{8,})["\']'), "secret"),
    (re.compile(r'sk-[A-Za-z0-9]{40,}'), "openai_key"),
    (re.compile(r'ghp_[A-Za-z0-9]{36}'), "github_pat"),
    (re.compile(r'AAAA[A-Za-z0-9_\-]{60,}'), "firebase_key"),
    (re.compile(r'-----BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY-----'), "private_key"),
    (re.compile(r'(?i)bearer\s+[A-Za-z0-9\-_\.]{20,}'), "bearer_token"),
]

SAFE_ALLOWLIST = ["testkey", "test_", "example", "placeholder", "YOUR_", "<", "TODO",
                  "storePassword", "keyPassword", "xrj45yWGLbsO7W0v"]


def _is_allowlisted(line: str) -> bool:
    return any(kw in line for kw in SAFE_ALLOWLIST)


def scan_secrets(paths: List[Path]) -> List[Dict]:
    findings = []
    for path in paths:
        try:
            text = path.read_text(errors="replace")
            for i, line in enumerate(text.split("\n"), 1):
                if _is_allowlisted(line):
                    continue
                for pat, label in SECRET_PATTERNS:
                    if pat.search(line):
                        findings.append({
                            "type": "secret",
                            "label": label,
                            "file": str(path.relative_to(REPO_ROOT)),
                            "line": i,
                            "severity": "critical",
                            "message": f"Potential {label} found",
                            "fix": f"Remove secret from {path.name} — use env vars or GitHub Secrets",
                        })
                        break
        except Exception:
            pass
    return findings


def scan_source_code() -> Dict:
    result = {"scanner": "source", "issues": []}
    src_dirs = [REPO_ROOT / "app" / "src", REPO_ROOT / "termux-shared" / "src"]
    java_files = []
    for d in src_dirs:
        if d.exists():
            java_files.extend(d.rglob("*.java"))
            java_files.extend(d.rglob("*.kt"))

    for jf in java_files:
        try:
            text = jf.read_text(errors="replace")
            rel = str(jf.relative_to(REPO_ROOT))

            # World-readable CVE
            if re.search(r'MODE_WORLD_READABLE|MODE_WORLD_WRITEABLE', text):
                result["issues"].append({
                    "type": "world_readable", "severity": "critical",
                    "file": rel, "message": "World-readable/writable file mode — critical CVE",
                    "fix": "Replace with MODE_PRIVATE (0x0000)",
                })

            # printStackTrace
            for m in re.finditer(r'\.printStackTrace\(\)', text):
                result["issues"].append({
                    "type": "debug_logging", "severity": "warning",
                    "file": rel, "message": "printStackTrace() in production code",
                    "fix": "Replace with Log.e(TAG, \"error\", e)",
                })
                break  # one per file

            # Runtime.exec without validation
            if re.search(r'Runtime\.getRuntime\(\)\.exec\(', text):
                result["issues"].append({
                    "type": "shell_injection_risk", "severity": "warning",
                    "file": rel, "message": "Runtime.exec() — verify input is sanitized",
                    "fix": "Validate/sanitize all args passed to Runtime.exec()",
                })

            # Deprecated APIs
            deprecated = [
                ("getColor(R.", "use ContextCompat.getColor()"),
                (".getText(R.",  "getText() is deprecated — use getString()"),
                ("new AsyncTask", "AsyncTask is deprecated — use coroutines or ExecutorService"),
            ]
            for pat, fix_msg in deprecated:
                if pat in text:
                    result["issues"].append({
                        "type": "deprecated_api", "severity": "info",
                        "file": rel, "message": f"Deprecated API: {pat.strip()}",
                        "fix": fix_msg,
                    })

        except Exception:
            pass

    return result


def scan_configuration() -> Dict:
    result = {"scanner": "config", "issues": []}

    manifest = REPO_ROOT / "app" / "src" / "main" / "AndroidManifest.xml"
    if manifest.exists():
        text = manifest.read_text(errors="replace")

        if 'android:debuggable="true"' in text:
            result["issues"].append({
                "type": "hardcoded_debuggable", "severity": "critical",
                "file": "app/src/main/AndroidManifest.xml",
                "message": "android:debuggable=true — MUST remove for production",
                "fix": "Remove android:debuggable from manifest — build system controls this",
            })

        if 'android:allowBackup="true"' in text:
            result["issues"].append({
                "type": "allow_backup", "severity": "warning",
                "file": "app/src/main/AndroidManifest.xml",
                "message": "android:allowBackup=true may expose user data",
                "fix": "Set android:allowBackup=false or configure backup rules",
            })

        if 'usesCleartextTraffic="true"' in text:
            result["issues"].append({
                "type": "cleartext_traffic", "severity": "warning",
                "file": "app/src/main/AndroidManifest.xml",
                "message": "Cleartext (HTTP) traffic allowed",
                "fix": "Remove usesCleartextTraffic or restrict to specific domains",
            })

    # gradle.properties checks
    gp = REPO_ROOT / "gradle.properties"
    if gp.exists():
        text = gp.read_text(errors="replace")
        min_sdk = re.search(r'^minSdkVersion\s*=\s*(\d+)', text, re.MULTILINE)
        target_sdk = re.search(r'^targetSdkVersion\s*=\s*(\d+)', text, re.MULTILINE)
        compile_sdk = re.search(r'^compileSdkVersion\s*=\s*(\d+)', text, re.MULTILINE)

        if min_sdk and target_sdk and compile_sdk:
            m, t, c = int(min_sdk.group(1)), int(target_sdk.group(1)), int(compile_sdk.group(1))
            if not (m <= t <= c):
                result["issues"].append({
                    "type": "sdk_chain_invalid", "severity": "critical",
                    "file": "gradle.properties",
                    "message": f"SDK chain invalid: min({m}) ≤ target({t}) ≤ compile({c}) must hold",
                    "fix": "Fix minSdkVersion ≤ targetSdkVersion ≤ compileSdkVersion",
                })

    return result


def scan_environment() -> Dict:
    result = {"scanner": "environment", "issues": []}

    # Required files
    required = [
        "app/build.gradle", "termux-shared/build.gradle",
        "terminal-emulator/build.gradle", "terminal-view/build.gradle",
        "settings.gradle", "build.gradle", "gradle.properties",
        "gradlew", "gradle/wrapper/gradle-wrapper.jar",
        "app/src/main/AndroidManifest.xml",
    ]
    for f in required:
        path = REPO_ROOT / f
        if not path.exists():
            result["issues"].append({
                "type": "missing_file", "severity": "critical",
                "file": f, "message": f"Required file missing: {f}",
                "fix": f"Restore {f} from git history or re-generate",
            })

    # gradlew executable
    gradlew = REPO_ROOT / "gradlew"
    if gradlew.exists() and not os.access(gradlew, os.X_OK):
        result["issues"].append({
            "type": "not_executable", "severity": "critical",
            "file": "gradlew", "message": "gradlew is not executable",
            "fix": "Run: chmod +x gradlew",
        })

    # Broken symlinks
    for path in REPO_ROOT.rglob("*"):
        if ".git" in str(path):
            continue
        if path.is_symlink() and not path.exists():
            result["issues"].append({
                "type": "broken_symlink", "severity": "warning",
                "file": str(path.relative_to(REPO_ROOT)),
                "message": f"Broken symlink: {path.name}",
                "fix": "Remove or fix the broken symlink",
            })

    return result


import os  # noqa — needed for os.access above


def scan_secrets_in_repo() -> Dict:
    result = {"scanner": "secrets", "issues": []}

    scan_paths = []
    extensions = {".java", ".kt", ".py", ".js", ".ts", ".sh", ".yaml", ".yml",
                  ".json", ".properties", ".gradle", ".xml", ".env", ".conf"}
    for path in REPO_ROOT.rglob("*"):
        if ".git" in str(path) or "build/" in str(path) or "node_modules" in str(path):
            continue
        if path.suffix in extensions and path.is_file():
            scan_paths.append(path)

    findings = scan_secrets(scan_paths)
    result["issues"].extend(findings)
    return result


def scan_build_readiness() -> Dict:
    result = {"scanner": "build_readiness", "issues": []}

    # Signing config
    build_gradle = REPO_ROOT / "app" / "build.gradle"
    if build_gradle.exists():
        text = build_gradle.read_text(errors="replace")
        if "signingConfigs" not in text:
            result["issues"].append({
                "type": "no_signing_config", "severity": "warning",
                "file": "app/build.gradle",
                "message": "No signingConfigs defined",
                "fix": "Add a debug signingConfig to app/build.gradle",
            })
        if "downloadBootstrap" not in text:
            result["issues"].append({
                "type": "no_bootstrap_task", "severity": "critical",
                "file": "app/build.gradle",
                "message": "Bootstrap download task missing — build will fail",
                "fix": "Add downloadBootstrap task with SHA-256 checksums",
            })

    # settings.gradle includes
    settings = REPO_ROOT / "settings.gradle"
    if settings.exists():
        text = settings.read_text(errors="replace")
        for mod in ["app", "terminal-emulator", "terminal-view", "termux-shared"]:
            if f":{mod}" not in text:
                result["issues"].append({
                    "type": "missing_module_include", "severity": "critical",
                    "file": "settings.gradle",
                    "message": f"Module ':{mod}' not included in settings.gradle",
                    "fix": f"Add: include ':{mod}' to settings.gradle",
                })

    return result


def main():
    parser = argparse.ArgumentParser(description="NasTech Health Bot")
    parser.add_argument("--profile", required=True)
    parser.add_argument("--output",  required=True)
    args = parser.parse_args()

    profile = json.loads(Path(args.profile).read_text())
    all_issues = []
    scanners = {}

    print("🏥 Running Health Scans...")
    for name, fn in [
        ("source",         scan_source_code),
        ("configuration",  scan_configuration),
        ("environment",    scan_environment),
        ("secrets",        scan_secrets_in_repo),
        ("build_readiness", scan_build_readiness),
    ]:
        print(f"  🔍 {name}...")
        r = fn()
        scanners[name] = r
        all_issues.extend(r.get("issues", []))

    critical = sum(1 for i in all_issues if i.get("severity") == "critical")
    warnings = sum(1 for i in all_issues if i.get("severity") == "warning")
    status = "fail" if critical > 0 else ("warn" if warnings > 0 else "pass")

    report = {
        "status":         status,
        "issues_found":   len(all_issues),
        "critical_count": critical,
        "warning_count":  warnings,
        "all_issues":     all_issues,
        "scanners":       scanners,
    }

    Path(args.output).write_text(json.dumps(report, indent=2))
    print(f"\n🏥 Health Scan Complete: {status.upper()}")
    print(f"   Critical: {critical}  Warnings: {warnings}  Total: {len(all_issues)}")
    print(f"✅ Health report saved: {args.output}")

    if critical > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
