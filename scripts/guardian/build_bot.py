#!/usr/bin/env python3
"""
NasTech Guardian — Build Bot (Stage 4)
Executes builds, captures logs, detects failure patterns, produces build report.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent.parent

# Error pattern matchers → human-readable diagnosis
ERROR_PATTERNS = [
    (re.compile(r'error:\s+(.+\.java):(\d+):\s+(.+)'),           "java_compile_error"),
    (re.compile(r'error:\s+(.+\.kt):(\d+):(\d+):\s+(.+)'),       "kotlin_compile_error"),
    (re.compile(r'Could not resolve\s+(.+)'),                     "dependency_resolution"),
    (re.compile(r'NDK build failed'),                             "ndk_build_fail"),
    (re.compile(r'Execution failed for task\s+[\'"](.+)[\'"]'),   "task_failure"),
    (re.compile(r'duplicate class\s+(.+)'),                       "duplicate_class"),
    (re.compile(r'Resource .+ already defined'),                  "duplicate_resource"),
    (re.compile(r'Manifest merger failed'),                       "manifest_merge"),
    (re.compile(r'minSdkVersion (\d+) cannot be smaller than'),   "sdk_conflict"),
    (re.compile(r'uses-sdk:minSdkVersion (\d+) cannot be smaller'), "sdk_conflict"),
    (re.compile(r'Gradle sync failed'),                           "gradle_sync"),
    (re.compile(r'BUILD FAILED'),                                 "build_failed"),
    (re.compile(r'OutOfMemoryError'),                             "oom"),
    (re.compile(r'Checksum (.+) for (.+) is invalid'),            "checksum_mismatch"),
    (re.compile(r'bootstrap-(.+)\.zip'),                          "bootstrap_issue"),
]


def classify_errors(log: str) -> list:
    """Extract and classify errors from build log."""
    found = []
    for pat, error_type in ERROR_PATTERNS:
        for m in pat.finditer(log):
            context_start = max(0, log.rfind("\n", 0, m.start()) + 1)
            context_end = log.find("\n", m.end())
            context_line = log[context_start:context_end].strip()
            found.append({
                "type":    error_type,
                "match":   m.group(0),
                "context": context_line,
                "fix":     FIXES.get(error_type, "Check build log for details"),
            })
    return found


FIXES = {
    "java_compile_error":     "Fix Java compile errors — check imports and API compatibility",
    "kotlin_compile_error":   "Fix Kotlin compile errors — check syntax and version",
    "dependency_resolution":  "Dependency resolution failed — check network and repository URLs",
    "ndk_build_fail":         "NDK build failed — check Android.mk and NDK version in gradle.properties",
    "task_failure":           "Gradle task failed — run ./gradlew <task> --stacktrace for details",
    "duplicate_class":        "Duplicate class — remove one of the conflicting dependencies",
    "duplicate_resource":     "Duplicate resource — check res/ folders for duplicate IDs",
    "manifest_merge":         "Manifest merge failed — check AndroidManifest.xml in all modules",
    "sdk_conflict":           "SDK version conflict — align minSdkVersion across all modules",
    "gradle_sync":            "Gradle sync failed — check build.gradle files",
    "build_failed":           "BUILD FAILED — see errors above for root cause",
    "oom":                    "Out of memory — increase org.gradle.jvmargs in gradle.properties",
    "checksum_mismatch":      "Bootstrap checksum mismatch — update SHA-256 checksums in app/build.gradle",
    "bootstrap_issue":        "Bootstrap zip issue — verify downloadBootstrap task and checksums",
}


def run_gradle_build(variant: str = "apt-android-7") -> dict:
    """Execute Gradle debug build and capture output."""
    env = os.environ.copy()
    env["TERMUX_PACKAGE_VARIANT"] = variant

    gradlew = REPO_ROOT / "gradlew"
    if not gradlew.exists():
        return {"success": False, "error": "gradlew not found", "log": ""}

    cmd = [str(gradlew), "assembleDebug", "--no-daemon", "--stacktrace", "--warning-mode", "all"]
    print(f"  ⚙️  Running: {' '.join(cmd)}")
    print(f"  📦 Variant: {variant}")

    t0 = time.time()
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
            timeout=600,
            env=env,
        )
        elapsed = round(time.time() - t0, 1)
        full_log = proc.stdout + "\n" + proc.stderr
        success = proc.returncode == 0

        return {
            "success":      success,
            "returncode":   proc.returncode,
            "elapsed_sec":  elapsed,
            "log":          full_log,
            "stdout":       proc.stdout[-5000:],  # last 5k chars
            "stderr":       proc.stderr[-3000:],
        }
    except subprocess.TimeoutExpired:
        return {
            "success": False,
            "error":   "Build timed out after 600 seconds",
            "log":     "TIMEOUT",
            "elapsed_sec": 600,
        }
    except Exception as e:
        return {"success": False, "error": str(e), "log": ""}


def find_apks() -> list:
    """Find produced APKs."""
    apk_dir = REPO_ROOT / "app" / "build" / "outputs" / "apk" / "debug"
    if not apk_dir.exists():
        return []
    apks = list(apk_dir.glob("*.apk"))
    return [{"name": a.name, "size_mb": round(a.stat().st_size / 1024 / 1024, 2)} for a in apks]


def run_lint() -> dict:
    """Run Android lint and return summary."""
    gradlew = REPO_ROOT / "gradlew"
    if not gradlew.exists():
        return {"status": "skip", "issues": 0}
    try:
        proc = subprocess.run(
            [str(gradlew), ":app:lint", "--no-daemon", "-q"],
            capture_output=True, text=True, cwd=REPO_ROOT, timeout=300
        )
        log = proc.stdout + proc.stderr
        errors   = len(re.findall(r'\bError\b', log))
        warnings = len(re.findall(r'\bWarning\b', log))
        return {
            "status":   "fail" if errors > 0 else "pass",
            "errors":   errors,
            "warnings": warnings,
            "snippet":  log[-1000:],
        }
    except subprocess.TimeoutExpired:
        return {"status": "timeout", "issues": 0}
    except Exception as e:
        return {"status": "error", "message": str(e), "issues": 0}


def run_unit_tests() -> dict:
    """Run unit tests and return summary."""
    gradlew = REPO_ROOT / "gradlew"
    if not gradlew.exists():
        return {"status": "skip"}
    try:
        proc = subprocess.run(
            [str(gradlew), "test", "--no-daemon", "-q"],
            capture_output=True, text=True, cwd=REPO_ROOT, timeout=300
        )
        success = proc.returncode == 0
        log = proc.stdout + proc.stderr
        failures = re.findall(r'FAILED$', log, re.MULTILINE)
        return {
            "status":   "pass" if success else "fail",
            "failures": len(failures),
            "log":      log[-2000:],
        }
    except subprocess.TimeoutExpired:
        return {"status": "timeout"}
    except Exception as e:
        return {"status": "error", "message": str(e)}


def main():
    parser = argparse.ArgumentParser(description="NasTech Build Bot")
    parser.add_argument("--profile", required=True)
    parser.add_argument("--output",  required=True)
    args = parser.parse_args()

    profile = json.loads(Path(args.profile).read_text())
    print("🔨 Running Build Bot...")

    # Run build
    print("\n[1/3] Running Gradle build...")
    build_result = run_gradle_build()

    # Classify errors
    errors = classify_errors(build_result.get("log", ""))
    error_types = list({e["type"] for e in errors})

    # Find APKs
    apks = find_apks() if build_result["success"] else []

    # Lint (skip if build failed — lint may depend on build)
    lint_result = {"status": "skip"}
    test_result = {"status": "skip"}
    if build_result["success"]:
        print("\n[2/3] Running lint...")
        lint_result = run_lint()
        print("\n[3/3] Running unit tests...")
        test_result = run_unit_tests()

    status = "pass" if build_result["success"] else "fail"
    failure_reason = ""
    if errors:
        primary = errors[0]
        failure_reason = f"{primary['type']}: {primary['context'][:200]}"

    report = {
        "status":          status,
        "build_success":   build_result["success"],
        "apk_produced":    len(apks) > 0,
        "apks":            apks,
        "error_count":     len(errors),
        "error_types":     error_types,
        "errors":          errors[:20],
        "failure_reason":  failure_reason,
        "elapsed_sec":     build_result.get("elapsed_sec"),
        "lint":            lint_result,
        "tests":           test_result,
        "build_log_tail":  build_result.get("stdout", "")[-2000:],
    }

    Path(args.output).write_text(json.dumps(report, indent=2))
    print(f"\n🔨 Build Bot Complete: {status.upper()}")
    print(f"   APKs produced: {len(apks)}")
    print(f"   Errors found:  {len(errors)}")
    print(f"✅ Build report saved: {args.output}")

    if status == "fail":
        sys.exit(1)


if __name__ == "__main__":
    main()
