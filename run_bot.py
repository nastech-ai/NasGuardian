#!/usr/bin/env python3
"""
NasTech Guardian Bot — Replit launcher
AI Coordinator (Groq → Gemini → OpenRouter) is the head.
Monitors all nastech-ai repositories.
"""
import os
import sys

# ── Secret mapping: Replit name → env name the bot expects ──────────
_MAPS = [
    ("GITHUB_PERSONAL_ACCESS_TOKEN", "GITHUB_TOKEN"),
    ("GITHUB_PERSONAL_ACCESS_TOKEN", "GH_TOKEN"),
]
for _src, _dst in _MAPS:
    _val = os.environ.get(_src, "")
    if _val and not os.environ.get(_dst):
        os.environ[_dst] = _val

# ── Default primary repo ─────────────────────────────────────────────
if not os.environ.get("GITHUB_REPO"):
    os.environ["GITHUB_REPO"] = "nastech-ai/NasTerminal"

# ── All nastech-ai repos the AI monitors ────────────────────────────
# Injected into WATCHED_REPOS so the bot's /repos and /dashboard
# commands know about every repository automatically.
ALL_NASTECH_REPOS = [
    # Core — own repos
    "nastech-ai/NasTerminal",
    "nastech-ai/NasGuardian",
    "nastech-ai/NasTech-Agent",
    "nastech-ai/NasTech-Agentdemo",
    "nastech-ai/NasTechSpace",
    "nastech-ai/NasWebUI",
    "nastech-ai/NasChat",
    "nastech-ai/NasBeat",
    "nastech-ai/NasMusic",
    "nastech-ai/NasModifier-Bot",
    "nastech-ai/NasDoor",
    "nastech-ai/NasGUI",
    "nastech-ai/NasTests",
    "nastech-ai/NasUX",
    "nastech-ai/NasUX-Packages",
    "nastech-ai/NasUX-api",
    "nastech-ai/Voices",
    "nastech-ai/NASWEB",
    "nastech-ai/Hermes-Agent-1",
    "nastech-ai/NasGemma",
    "nastech-ai/stripeAI",
    "nastech-ai/bantu",
    "nastech-ai/nastech",
    "nastech-ai/awesome-nastech",
    "nastech-ai/testapp",
    "nastech-ai/testing",
    # Forks being developed
    "nastech-ai/n8n",
    "nastech-ai/dify",
    "nastech-ai/open-webui",
    "nastech-ai/crewAI",
    "nastech-ai/ollama",
    "nastech-ai/vllm",
    "nastech-ai/cline",
    "nastech-ai/supabase",
    "nastech-ai/vscode",
    "nastech-ai/rustdesk",
    "nastech-ai/social-app",
    "nastech-ai/flutter",
    "nastech-ai/happy-cli",
    "nastech-ai/happy-server",
    "nastech-ai/hermes-studio",
    "nastech-ai/hermes-workspace",
    "nastech-ai/Kimi-K2",
    "nastech-ai/AutoGPT",
    "nastech-ai/OpenHands",
    "nastech-ai/memex",
]

if not os.environ.get("WATCHED_REPOS"):
    os.environ["WATCHED_REPOS"] = ",".join(ALL_NASTECH_REPOS)

# ── Wire bot scripts directory ────────────────────────────────────────
_root = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_root, "scripts", "telegram_bot"))
sys.path.insert(0, os.path.join(_root, "scripts", "guardian"))
sys.path.insert(0, os.path.join(_root, "scripts"))

# ── Startup banner ───────────────────────────────────────────────────
print("🛡️  NasTech Guardian Bot — starting…")
_REQUIRED = [
    "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
    "GROQ_API_KEY", "GEMINI_API_KEY", "OPENROUTER_API_KEY", "GITHUB_TOKEN",
]
_all_ok = True
for _k in _REQUIRED:
    _v = os.environ.get(_k, "")
    _ok = bool(_v)
    _all_ok = _all_ok and _ok
    print(f"  {'✅' if _ok else '❌'} {_k}{': set' if _ok else ': NOT SET — bot may fail'}")

print(f"\n  📡 Watching {len(ALL_NASTECH_REPOS)} nastech-ai repos")
print(f"  🤖 AI head: Groq → Gemini → OpenRouter\n")

if not _all_ok:
    print("⚠️  Some secrets missing — set them in Replit Secrets then restart.\n")

# ── Launch ────────────────────────────────────────────────────────────
import importlib.util, pathlib

_bot_path = pathlib.Path(_root) / "scripts" / "telegram_bot" / "nastech_guardian_bot.py"
_spec = importlib.util.spec_from_file_location("nastech_guardian_bot", _bot_path)
_mod  = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
_mod.main()
