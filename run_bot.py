#!/usr/bin/env python3
"""NasTech Guardian Bot — Replit launcher"""
import os, sys

# Map Replit secret names → what the bot expects
# NOTE: do NOT use a dict here — duplicate source keys get silently dropped.
_MAPS = [
    ("GITHUB_PERSONAL_ACCESS_TOKEN", "GITHUB_TOKEN"),
    ("GITHUB_PERSONAL_ACCESS_TOKEN", "GH_TOKEN"),
]
for src, dst in _MAPS:
    val = os.environ.get(src, "")
    if val and not os.environ.get(dst):
        os.environ[dst] = val

# Set default repo
if not os.environ.get("GITHUB_REPO"):
    os.environ["GITHUB_REPO"] = "nastech-ai/NasTerminal"

# Add bot directory to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "scripts", "telegram_bot"))

print("🛡️  NasTech Guardian Bot — starting…")
for k in ["TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
          "GROQ_API_KEY", "GEMINI_API_KEY", "OPENROUTER_API_KEY", "GITHUB_TOKEN"]:
    val = os.environ.get(k, "")
    print(f"  {'✅' if val else '⚠️ '} {k}{': set' if val else ': NOT SET'}")
print()

# Run bot
exec(open("scripts/telegram_bot/nastech_guardian_bot.py").read())
