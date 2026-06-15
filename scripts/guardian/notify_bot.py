#!/usr/bin/env python3
"""
NasTech Guardian — Notification Bot (Stage 7)
Sends Telegram summary + posts GitHub summary.
One clean summary message instead of spamming every stage.
"""

import argparse
import json
import os
import sys
import urllib.request
import urllib.error
from pathlib import Path
from datetime import datetime, timezone

REPO_ROOT = Path(__file__).parent.parent.parent

STAGE_NAMES = {
    "VERIFY_STATUS":   "0 · Verify Org",
    "IDENTITY_STATUS": "1 · Identity",
    "DEP_STATUS":      "2 · Dependencies",
    "HEALTH_STATUS":   "3 · Health",
    "BUILD_STATUS":    "4 · Build",
    "REPAIR_STATUS":   "5 · Repair",
    "RELEASE_STATUS":  "6 · Release",
}


def status_icon(s: str) -> str:
    mapping = {
        "pass":    "✅", "success": "✅",
        "fail":    "❌", "failure": "❌",
        "warn":    "⚠️",
        "skipped": "⏭️",
        "partial": "🔧",
    }
    return mapping.get((s or "").lower(), "⚠️")


def send_telegram(token: str, chat_id: str, text: str, parse_mode: str = "HTML") -> bool:
    """Send a message via Telegram Bot API."""
    url  = f"https://api.telegram.org/bot{token}/sendMessage"
    body = json.dumps({
        "chat_id":    chat_id,
        "text":       text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": True,
    }).encode("utf-8")
    req = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": "application/json"},
        method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            return result.get("ok", False)
    except Exception as e:
        print(f"[Telegram] Send failed: {e}", file=sys.stderr)
        return False


def build_telegram_message(statuses: dict, meta: dict) -> str:
    """Build the single Telegram summary message (HTML format)."""
    repo     = meta.get("repo", "unknown")
    sha      = meta.get("sha", "")[:7]
    run_url  = meta.get("run_url", "")
    repair   = meta.get("repair_pr", "")
    release  = meta.get("release_url", "")
    tag      = meta.get("release_tag", "")
    ts       = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    # Determine overall state
    all_vals = list(statuses.values())
    has_fail = any(v in ("fail", "failure") for v in all_vals)
    has_warn = any(v in ("warn",) for v in all_vals)
    overall  = "❌ FAILED" if has_fail else ("⚠️ WARNING" if has_warn else "✅ COMPLETE")

    lines = [
        f"<b>🛡️ NasTech Guardian</b>",
        f"<code>{repo}</code> · <code>{sha}</code>",
        f"<i>{ts}</i>",
        "",
        f"<b>Overall: {overall}</b>",
        "",
        "<b>Pipeline Stages:</b>",
    ]

    stage_order = [
        ("VERIFY_STATUS",   "0 · Verify Org"),
        ("IDENTITY_STATUS", "1 · Identity"),
        ("DEP_STATUS",      "2 · Dependencies"),
        ("HEALTH_STATUS",   "3 · Health"),
        ("BUILD_STATUS",    "4 · Build"),
        ("REPAIR_STATUS",   "5 · Repair"),
        ("RELEASE_STATUS",  "6 · Release"),
    ]
    for key, label in stage_order:
        val = statuses.get(key, "skipped")
        icon = status_icon(val)
        lines.append(f"  {icon} {label}: <code>{val or 'skipped'}</code>")

    if repair:
        lines += ["", f"🔧 <b>Repair PR:</b> <a href='{repair}'>View PR</a>"]
    if release and tag:
        lines += [f"🚀 <b>Release:</b> <a href='{release}'>{tag}</a>"]
    if run_url:
        lines += ["", f"📋 <a href='{run_url}'>Full Pipeline Log</a>"]

    if has_fail:
        lines += ["", "⚠️ <b>Action Required:</b> Review failures and retry or approve repair PR."]

    return "\n".join(lines)


def build_github_summary(statuses: dict, meta: dict) -> str:
    """Build the GitHub Actions step summary (Markdown)."""
    repo    = meta.get("repo", "unknown")
    sha     = meta.get("sha", "")[:7]
    run_url = meta.get("run_url", "")
    repair  = meta.get("repair_pr", "")
    release = meta.get("release_url", "")
    tag     = meta.get("release_tag", "")
    ts      = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    all_vals = list(statuses.values())
    has_fail = any(v in ("fail", "failure") for v in all_vals if v)
    final    = "❌ FAILED" if has_fail else "✅ COMPLETE"

    lines = [
        f"# 🛡️ NasTech Guardian — {final}",
        f"**Repository:** `{repo}` · **Commit:** `{sha}` · *{ts}*",
        "",
        "## Pipeline Results",
        "| Stage | Status |",
        "|-------|--------|",
    ]

    stage_order = [
        ("VERIFY_STATUS",   "0 · Verify Organization"),
        ("IDENTITY_STATUS", "1 · Identity Bot"),
        ("DEP_STATUS",      "2 · Dependency Bot"),
        ("HEALTH_STATUS",   "3 · Health Bot"),
        ("BUILD_STATUS",    "4 · Build Bot"),
        ("REPAIR_STATUS",   "5 · Repair Bot"),
        ("RELEASE_STATUS",  "6 · Release Bot"),
    ]
    for key, label in stage_order:
        val  = statuses.get(key, "skipped")
        icon = status_icon(val)
        lines.append(f"| {label} | {icon} `{val or 'skipped'}` |")

    lines += [""]
    if repair:
        lines.append(f"🔧 **Repair PR:** {repair}")
    if release and tag:
        lines.append(f"🚀 **Release:** [{tag}]({release})")
    if run_url:
        lines.append(f"📋 [Full Pipeline Log]({run_url})")

    lines += [
        "",
        "---",
        "*NasTech Guardian · Sequential State Machine Orchestrator*",
        "*One Trigger · One Stage · One Result · Then Launch Next*",
    ]
    return "\n".join(lines)


def determine_final_state(statuses: dict) -> str:
    vals = list(statuses.values())
    if any(v in ("fail", "failure") for v in vals if v):
        return "FAILED"
    if any(v in ("warn",) for v in vals if v):
        return "COMPLETE_WITH_WARNINGS"
    return "COMPLETE"


def main():
    parser = argparse.ArgumentParser(description="NasTech Notification Bot")
    parser.add_argument("--repo",    required=True)
    parser.add_argument("--sha",     required=True)
    parser.add_argument("--run-url", required=True)
    parser.add_argument("--output",  required=True)
    args = parser.parse_args()

    tg_token  = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    tg_chat   = os.environ.get("TELEGRAM_CHAT_ID",   "")

    statuses = {
        "VERIFY_STATUS":   os.environ.get("VERIFY_STATUS", ""),
        "IDENTITY_STATUS": os.environ.get("IDENTITY_STATUS", ""),
        "DEP_STATUS":      os.environ.get("DEP_STATUS", ""),
        "HEALTH_STATUS":   os.environ.get("HEALTH_STATUS", ""),
        "BUILD_STATUS":    os.environ.get("BUILD_STATUS", ""),
        "REPAIR_STATUS":   os.environ.get("REPAIR_STATUS", ""),
        "RELEASE_STATUS":  os.environ.get("RELEASE_STATUS", ""),
    }
    meta = {
        "repo":        args.repo,
        "sha":         args.sha,
        "run_url":     args.run_url,
        "repair_pr":   os.environ.get("REPAIR_PR",   ""),
        "release_url": os.environ.get("RELEASE_URL", ""),
        "release_tag": os.environ.get("RELEASE_TAG", ""),
    }

    print("📣 Running Notification Bot...")
    print(f"   Statuses: {json.dumps(statuses)}")

    # Telegram
    tg_sent = False
    if tg_token and tg_chat:
        print("\n[1/2] Sending Telegram notification...")
        msg = build_telegram_message(statuses, meta)
        tg_sent = send_telegram(tg_token, tg_chat, msg)
        print(f"   Telegram: {'✅ sent' if tg_sent else '❌ failed'}")
    else:
        print("\n[1/2] Telegram skipped (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set)")

    # GitHub summary
    print("\n[2/2] Writing GitHub summary...")
    summary_md = build_github_summary(statuses, meta)
    summary_path = Path(os.environ.get("GITHUB_STEP_SUMMARY", "/dev/null"))
    try:
        summary_path.write_text(summary_md)
        print("   ✅ GitHub summary written")
    except Exception as e:
        print(f"   ⚠️  Could not write GitHub summary: {e}")

    final_state = determine_final_state(statuses)

    report = {
        "status":       "pass",
        "final_state":  final_state,
        "telegram_sent": tg_sent,
        "statuses":     statuses,
    }
    Path(args.output).write_text(json.dumps(report, indent=2))
    print(f"\n📣 Notification Bot Complete. Final state: {final_state}")
    print(f"✅ Notify report saved: {args.output}")


if __name__ == "__main__":
    main()
