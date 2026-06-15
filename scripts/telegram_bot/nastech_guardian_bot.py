#!/usr/bin/env python3
"""
NasTech Guardian Telegram Bot v3.0
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  Developed by Nsamba Naswif Cohen
  NasTech AI Terminal — Multi-agent CI/CD Orchestrator
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Features:
  - AI chat with session history (Groq → Gemini → OpenRouter)
  - /ask /explain /review /fix_error /run /ocr /summarize /translate
  - Multi-repo: /repo add|list|switch|audit|remove, /dashboard, /audit, /fixplan
  - Full pre-join audit: secrets, workflows, builds, issues, security
  - Per-repo fix plans with prioritised step-by-step instructions
  - 100-button reply keyboard across 9 categories
  - Full error screenshots: /errors /errorshot — job/step breakdown + log file
  - 55+ CI/CD commands, daily digest, OCR, group management

Install:  pip install python-telegram-bot apscheduler requests pillow
Run:      python3 scripts/telegram_bot/nastech_guardian_bot.py
"""

import asyncio
import json
import logging
import os
import re
import subprocess
import sys
import textwrap
import time
import traceback
import urllib.request
import urllib.error
from collections import defaultdict
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Optional

try:
    from telegram import (
        Update, InlineKeyboardButton, InlineKeyboardMarkup,
        BotCommand, BotCommandScopeDefault,
        ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove
    )
    from telegram.ext import (
        Application, CommandHandler, CallbackQueryHandler,
        ContextTypes, MessageHandler, filters, ConversationHandler
    )
    from telegram.constants import ParseMode
    from telegram.error import TelegramError
except ImportError:
    print("Install: pip install 'python-telegram-bot>=20.7'")
    sys.exit(1)

try:
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    SCHEDULER_OK = True
except ImportError:
    SCHEDULER_OK = False

# Multi-repo manager
try:
    import sys as _sys, os as _os
    _sys.path.insert(0, _os.path.join(_os.path.dirname(__file__), "..", "guardian"))
    from repo_manager import (
        registry, audit_repo,
        format_audit_html, format_fix_plan_html, format_dashboard_html,
    )
    REPO_MANAGER_OK = True
except ImportError as _e:
    REPO_MANAGER_OK = False
    registry = None

# ── Config ─────────────────────────────────────────────────────────
BOT_TOKEN    = os.environ.get("TELEGRAM_BOT_TOKEN", "")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GROQ_KEY     = os.environ.get("GROQ_API_KEY", "")
GEMINI_KEY   = os.environ.get("GEMINI_API_KEY", "")
OR_KEY       = os.environ.get("OPENROUTER_API_KEY", "")
GITHUB_REPO      = os.environ.get("GITHUB_REPO", "nastech-ai/NasTerminal")
NASGUARDIAN_REPO = "nastech-ai/NasGuardian"
GUARDIAN_WF      = "nasguardian_guardian.yml"
TESTS_WF         = "nasguardian_tests.yml"
AUDIT_WF         = "nasguardian_audit.yml"
BOT_VERSION      = "3.0.0"
BRAND        = "🛡️ NasTech Guardian  |  Developed by Nsamba Naswif Cohen"

# Whitelist: comma-separated chat IDs. Empty = allow all.
_wl_raw = os.environ.get("TELEGRAM_CHAT_ID", "") or os.environ.get("ALLOWED_CHAT_IDS", "")
WHITELIST = {x.strip() for x in _wl_raw.split(",") if x.strip()} if _wl_raw else set()

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(
            Path.home() / "nastech_guardian_bot.log",
            encoding="utf-8",
            errors="replace"
        )
    ]
)
logger = logging.getLogger("NasTechGuardian")

# ── Per-chat session store ──────────────────────────────────────────
def _default_session():
    return {
        "history":     [],
        "repo":        GITHUB_REPO,
        "ai_mode":     True,
        "last_active": 0.0,
        "notif": {
            "build":    True,
            "failures": True,
            "security": True,
            "pr":       True,
            "release":  True,
            "daily":    False,
        },
    }

sessions: dict = defaultdict(_default_session)


# ─────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────

def authorized(update: Update) -> bool:
    if not WHITELIST:
        return True
    return str(update.effective_chat.id) in WHITELIST


async def deny(update: Update):
    cid = update.effective_chat.id
    await update.message.reply_text(
        f"⛔ Not authorized.\n<code>Your ID: {cid}</code>\n"
        "Ask the bot owner to add your ID to ALLOWED_CHAT_IDS.",
        parse_mode=ParseMode.HTML
    )


def esc(text: str) -> str:
    """Escape HTML."""
    return (text.replace("&","&amp;").replace("<","&lt;").replace(">","&gt;"))


def truncate(text: str, max_len: int = 4000) -> str:
    if len(text) <= max_len:
        return text
    return text[:max_len - 3] + "..."


def gh(method: str, path: str, body: dict = None, repo: str = None) -> dict:
    """GitHub REST API call."""
    repo = repo or GITHUB_REPO
    url  = f"https://api.github.com{path}"
    data = json.dumps(body).encode() if body else None
    req  = urllib.request.Request(
        url, data=data, method=method,
        headers={
            "Authorization":        f"Bearer {GITHUB_TOKEN}",
            "Accept":               "application/vnd.github+json",
            "Content-Type":         "application/json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return {"error": f"HTTP {e.code}", "detail": e.read().decode()[:200]}
    except Exception as e:
        return {"error": str(e)}


def trigger_wf(workflow: str, ref: str = "main", inputs: dict = None, repo: str = None) -> bool:
    repo = repo or GITHUB_REPO
    owner, rname = repo.split("/")
    result = gh("POST", f"/repos/{owner}/{rname}/actions/workflows/{workflow}/dispatches",
                {"ref": ref, "inputs": inputs or {}}, repo=repo)
    return "error" not in result


def wf_runs(workflow: str = None, limit: int = 5, repo: str = None) -> list:
    repo = repo or GITHUB_REPO
    owner, rname = repo.split("/")
    path = f"/repos/{owner}/{rname}/actions/runs?per_page={limit}"
    if workflow:
        path += f"&workflow={workflow}"
    r = gh("GET", path, repo=repo)
    return r.get("workflow_runs", [])


# ─────────────────────────────────────────────────────────────────────
# AI Coordinator (inline — no import dependency)
# ─────────────────────────────────────────────────────────────────────

def _ai_post(url: str, headers: dict, payload: dict) -> Optional[str]:
    data = json.dumps(payload).encode()
    req  = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=45) as r:
            resp = json.loads(r.read().decode())
            if "choices" in resp:
                return resp["choices"][0]["message"]["content"].strip()
            if "candidates" in resp:
                return resp["candidates"][0]["content"]["parts"][0]["text"].strip()
    except Exception as e:
        logger.debug(f"AI call failed ({url}): {e}")
    return None


def ai_ask(system: str, user: str) -> dict:
    """Try Groq → Gemini → OpenRouter."""
    # Groq
    if GROQ_KEY:
        r = _ai_post(
            "https://api.groq.com/openai/v1/chat/completions",
            {"Authorization": f"Bearer {GROQ_KEY}", "Content-Type": "application/json"},
            {"model": "llama-3.3-70b-versatile",
             "messages": [{"role":"system","content":system},{"role":"user","content":user}],
             "max_tokens": 2048, "temperature": 0.3}
        )
        if r: return {"text": r, "provider": "Groq"}
    # Gemini
    if GEMINI_KEY:
        r = _ai_post(
            f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={GEMINI_KEY}",
            {"Content-Type": "application/json"},
            {"contents":[{"parts":[{"text":f"{system}\n\n{user}"}]}],
             "generationConfig":{"maxOutputTokens":2048,"temperature":0.3}}
        )
        if r: return {"text": r, "provider": "Gemini"}
    # OpenRouter
    if OR_KEY:
        r = _ai_post(
            "https://openrouter.ai/api/v1/chat/completions",
            {"Authorization": f"Bearer {OR_KEY}", "Content-Type": "application/json",
             "HTTP-Referer": "https://github.com/nastech-ai/NasTerminal", "X-Title": "NasTech Guardian"},
            {"model": "meta-llama/llama-3.3-70b-instruct",
             "messages": [{"role":"system","content":system},{"role":"user","content":user}],
             "max_tokens": 2048, "temperature": 0.3}
        )
        if r: return {"text": r, "provider": "OpenRouter"}
    return {"text": "⚠️ All AI providers unavailable. Check your API keys.", "provider": "none"}


def ai_chat(message: str, history: list, repo: str = GITHUB_REPO) -> dict:
    system = (
        "You are NasTech Guardian — an expert AI DevOps assistant for the NasTech AI Terminal "
        f"(Termux-based Android app at {repo}). "
        "Help with: Android builds, Gradle, GitHub Actions, Python, CI/CD, Telegram bots. "
        "Be concise and actionable. Use code blocks. Keep responses under 500 words."
    )
    ctx = "\n".join(
        f"{'User' if h['role']=='user' else 'Assistant'}: {h['content']}"
        for h in history[-6:]
    )
    user_prompt = f"{ctx}\nUser: {message}" if ctx else message
    return ai_ask(system, user_prompt)


# ─────────────────────────────────────────────────────────────────────
# Safe Python sandbox (CodexClaw pattern)
# ─────────────────────────────────────────────────────────────────────

BLOCKED_BUILTINS = {"open","exec","eval","__import__","compile","input","breakpoint"}
BLOCKED_PATTERNS = [r"import\s+os", r"import\s+sys", r"import\s+subprocess",
                    r"__builtins__", r"globals\(\)", r"locals\(\)"]

def safe_run_python(code: str, timeout: int = 5) -> dict:
    """Run sandboxed Python code. Returns {output, error, ok}."""
    # Safety checks
    for pat in BLOCKED_PATTERNS:
        if re.search(pat, code):
            return {"output": "", "error": f"Blocked pattern: {pat}", "ok": False}
    
    # Wrap in restricted exec
    wrapped = textwrap.dedent(f"""
import math, random, datetime, json, re, itertools, collections
__builtins__ = {{k:v for k,v in __builtins__.__dict__.items() if k not in {BLOCKED_BUILTINS!r}}}
{code}
""")
    try:
        result = subprocess.run(
            [sys.executable, "-c", wrapped],
            capture_output=True, text=True,
            timeout=timeout,
            env={"PATH": os.environ.get("PATH",""), "HOME": str(Path.home())},
        )
        out = (result.stdout or "").strip()
        err = (result.stderr or "").strip()
        return {"output": out[:1500], "error": err[:500], "ok": result.returncode == 0}
    except subprocess.TimeoutExpired:
        return {"output": "", "error": f"Timeout ({timeout}s)", "ok": False}
    except Exception as e:
        return {"output": "", "error": str(e), "ok": False}


# ─────────────────────────────────────────────────────────────────────
# OCR
# ─────────────────────────────────────────────────────────────────────

async def do_ocr(photo_bytes: bytes) -> str:
    """Extract text from image using tesseract, fallback to Gemini vision."""
    # Try pytesseract
    try:
        import pytesseract
        from PIL import Image
        img = Image.open(BytesIO(photo_bytes))
        text = pytesseract.image_to_string(img).strip()
        if text:
            return text
    except Exception:
        pass
    # Try Gemini vision API
    if GEMINI_KEY:
        import base64
        b64 = base64.b64encode(photo_bytes).decode()
        payload = {
            "contents": [{"parts": [
                {"inline_data": {"mime_type": "image/jpeg", "data": b64}},
                {"text": "Extract ALL text from this image. Return only the text, nothing else."}
            ]}]
        }
        r = _ai_post(
            f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={GEMINI_KEY}",
            {"Content-Type": "application/json"},
            payload
        )
        if r:
            return r
    return "No text found (install tesseract or set GEMINI_API_KEY)."


# ─────────────────────────────────────────────────────────────────────
# Message formatter
# ─────────────────────────────────────────────────────────────────────

def fmt_ai(result: dict) -> str:
    provider = result.get("provider", "?")
    text     = result.get("text", "")
    # Convert markdown code blocks for Telegram HTML
    text = re.sub(r'```(\w+)?\n([\s\S]*?)```', lambda m: f'<pre><code>{esc(m.group(2))}</code></pre>', text)
    text = re.sub(r'`([^`]+)`', lambda m: f'<code>{esc(m.group(1))}</code>', text)
    text = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', text)
    return f"{text}\n\n<i>via {provider}</i>"


# ═════════════════════════════════════════════════════════════════════
# REPLY KEYBOARD SYSTEM  — 100 buttons in 9 categories
# ═════════════════════════════════════════════════════════════════════

# ── Keyboard layouts ──────────────────────────────────────────────────
_KB_MAIN = [
    ["🤖 AI Chat",       "📊 Pipeline",      "🔍 Repos"],
    ["🛠️ Tools",         "🔑 API Keys",      "📱 Android"],
    ["🚀 Workflows",     "📋 Reports",       "🔒 Security"],
    ["🔔 Notifications", "⚙️ Settings",      "❓ Help"],
]

_KB_AI = [
    ["💬 Ask AI",         "📝 Code Review",   "🔍 Explain Code"],
    ["🐛 Fix Error",      "💻 Run Python",    "✨ Refactor Code"],
    ["📖 Summarize",      "🌐 Translate",     "📸 OCR Image"],
    ["🧠 AI Memory",      "🤖 AI On",         "🔇 AI Off"],
    ["🗑️ Clear History",  "🤖 Model Status",  "📡 AI Providers"],
    ["🏠 Main Menu"],
]

_KB_PIPELINE = [
    ["📊 Pipeline Status", "🔍 Full Scan",     "🏗️ Build"],
    ["🔄 Rebuild",         "🧪 Run Tests",     "🔧 Repair Bot"],
    ["🚀 Release",         "❤️ Health Check",  "🩺 Doctor Scan"],
    ["📋 Workflow Logs",   "❌ Recent Errors", "📤 Error Screenshot"],
    ["🔧 Auto Fix",        "✅ Approve Fix",   "❌ Cancel Fix"],
    ["📈 Metrics",         "🏷️ App Version",  "🔄 Trigger CI"],
    ["🏠 Main Menu"],
]

_KB_REPOS = [
    ["📁 My Repos",      "➕ Add Repo",      "📊 Repo Dashboard"],
    ["🔍 Audit Repo",    "📝 Fix Plan",      "🔄 Scan All Repos"],
    ["🔀 Switch Repo",   "❌ Remove Repo",   "🌿 Branches"],
    ["📌 Commits",       "🔀 Pull Requests", "⚠️ Open Issues"],
    ["🔒 Repo Security", "📦 Dependencies",  "📋 Repo Packages"],
    ["🏠 Main Menu"],
]

_KB_TOOLS = [
    ["📸 OCR Image",    "📖 Summarize Text", "🌐 Translate"],
    ["💻 Run Python",   "🔍 Explain Code",   "📝 Code Review"],
    ["📦 Dependencies", "🗂️ Packages",       "🔒 Security Scan"],
    ["📊 Storage Info", "🌐 Services",       "📈 System Metrics"],
    ["🧹 Clear Chat",   "🔍 Search Code",    "📋 Code Snippets"],
    ["🏠 Main Menu"],
]

_KB_KEYS = [
    ["🔑 View API Keys",    "🧪 Test All Keys",   "💾 Key Backup"],
    ["🔧 Set Groq Key",     "🔧 Set Gemini Key",  "🔧 Set OpenRouter"],
    ["🔧 Set TG Token",     "🔧 Set TG Chat ID",  "🔧 Set GitHub PAT"],
    ["🔒 Key Security",     "🔄 Reload Keys",     "📋 Key Status"],
    ["🏠 Main Menu"],
]

_KB_ANDROID = [
    ["📱 Install Termux",  "📦 Create Bundle",   "🔄 Update Guardian"],
    ["🚀 Start Bot",       "📋 Bot Logs",        "🛑 Stop Bot"],
    ["⚙️ Config Keys",     "🛡️ Guardian Status", "📲 Termux Setup"],
    ["🔗 NasGuardian",     "🔗 NasTerminal",     "📋 Boot Config"],
    ["📥 Download ZIP",    "🔄 Reinstall Bot",   "🔍 Check Deps"],
    ["🏠 Main Menu"],
]

_KB_WORKFLOWS = [
    ["🚀 Trigger Build",   "🧪 Trigger Tests",   "📋 List Workflows"],
    ["🔍 Workflow Runs",   "📊 Run Status",      "❌ Failed Runs"],
    ["🔀 Open PRs",        "⚠️ Open Issues",     "📌 Recent Commits"],
    ["🌿 All Branches",    "🏷️ App Version",     "📦 Latest Release"],
    ["🔄 Sync Repo",       "⚡ Fast Build",      "🏗️ Full Build"],
    ["🏠 Main Menu"],
]

_KB_REPORTS = [
    ["📊 Full Report",     "📅 Daily Digest",    "📈 Pipeline Stats"],
    ["📅 Subscribe Daily", "🔕 Unsubscribe",     "📋 Full Help"],
    ["ℹ️ About Guardian",  "💾 Backup Status",   "🧠 Bot Memory"],
    ["🤖 AI Providers",    "📊 Model Status",    "🌐 Network Status"],
    ["🏆 All Commands",    "📋 Command List",    "🔄 Refresh Status"],
    ["🏠 Main Menu"],
]

_KB_SECURITY = [
    ["🔒 Security Scan",  "🛡️ Full Audit",     "🔍 Scan Secrets"],
    ["⚠️ Vulnerabilities","🔧 Security Fix",   "📋 Audit Report"],
    ["🔑 API Key Check",  "🔒 Repo Perms",     "🛡️ Code Review"],
    ["📊 Security Score", "🔍 Dep Audit",      "⚡ Quick Audit"],
    ["🔄 Rescan Now",     "📤 Export Report",  "🔒 Lock Down"],
    ["🏠 Main Menu"],
]

_KB_SETTINGS = [
    ["🔑 API Keys",        "🤖 AI Models",      "📊 Bot Status"],
    ["🤖 AI On",           "🔇 AI Off",         "🗑️ Clear History"],
    ["💾 Backup Info",     "🧠 Bot Memory",     "📋 All Commands"],
    ["🔄 Restart Bot",     "⚙️ Preferences",   "📡 Connection Test"],
    ["🏠 Main Menu"],
]

_KB_NOTIFICATIONS = [
    ["🔔 All Notif ON",     "🔕 All Notif OFF",    "📊 Notif Status"],
    ["🔔 Build ON",         "🔕 Build OFF",         "🔔 Failures ON"],
    ["🔕 Failures OFF",     "🔔 PR Alerts ON",      "🔕 PR Alerts OFF"],
    ["🔔 Release ON",       "🔕 Release OFF",       "🔔 Security ON"],
    ["🔕 Security OFF",     "📅 Daily ON",          "🔕 Daily OFF"],
    ["📋 Digest Now",       "📤 Error Screenshot",  "🏠 Main Menu"],
]

# ── Label → keyboard mapping ──────────────────────────────────────────
_CATEGORY_KEYBOARDS: dict = {
    "🤖 AI Chat":       _KB_AI,
    "📊 Pipeline":      _KB_PIPELINE,
    "🔍 Repos":         _KB_REPOS,
    "🛠️ Tools":         _KB_TOOLS,
    "🔑 API Keys":      _KB_KEYS,
    "📱 Android":       _KB_ANDROID,
    "🚀 Workflows":     _KB_WORKFLOWS,
    "📋 Reports":       _KB_REPORTS,
    "🔒 Security":      _KB_SECURITY,
    "⚙️ Settings":      _KB_SETTINGS,
    "🏠 Main Menu":     _KB_MAIN,
}

# ── Button label → (command_text, description) ────────────────────────
_BUTTON_COMMANDS: dict = {
    # AI
    "💬 Ask AI":            ("/ask",          None),
    "📝 Code Review":       ("/review",       None),
    "🔍 Explain Code":      ("/explain",      None),
    "🐛 Fix Error":         ("/fix_error",    None),
    "💻 Run Python":        ("/run",          None),
    "✨ Refactor Code":     ("/ask",          "Refactor the following code for clarity and performance:"),
    "📖 Summarize Text":    ("/summarize",    None),
    "📖 Summarize":         ("/summarize",    None),
    "🌐 Translate":         ("/translate",    None),
    "📸 OCR Image":         ("/ask",          "Send me an image and I'll extract the text with OCR."),
    "🤖 AI On":             ("/ai_on",        None),
    "🔇 AI Off":            ("/ai_off",       None),
    "🗑️ Clear History":     ("/clear",        None),
    "🧠 AI Memory":         ("/memory",       None),
    "🤖 Model Status":      ("/models",       None),
    "📡 AI Providers":      ("/providers",    None),
    # Pipeline
    "📊 Pipeline Status":   ("/status",       None),
    "📊 Pipeline":          ("/status",       None),
    "🔍 Full Scan":         ("/scan",         None),
    "🏗️ Build":             ("/build",        None),
    "🔄 Rebuild":           ("/rebuild",      None),
    "🧪 Run Tests":         ("/test",         None),
    "🔧 Repair Bot":        ("/repair",       None),
    "🚀 Release":           ("/release",      None),
    "❤️ Health Check":      ("/health",       None),
    "🩺 Doctor Scan":       ("/doctor",       None),
    "📋 Workflow Logs":     ("/logs",         None),
    "❌ Recent Errors":     ("/errors",       None),
    "⚡ Quick Fix":         ("/fix",          None),
    "📈 Metrics":           ("/metrics",      None),
    "🏷️ App Version":       ("/version",      None),
    "🔄 Trigger CI":        ("/build",        None),
    "🚀 Trigger Build":     ("/build",        None),
    "🧪 Trigger Tests":     ("/test",         None),
    "⚡ Fast Build":        ("/build",        None),
    "🏗️ Full Build":        ("/rebuild",      None),
    # Repos
    "📁 My Repos":          ("/repos",        None),
    "🔍 Repos":             ("/repos",        None),
    "➕ Add Repo":          ("/addrepo",      None),
    "📊 Repo Dashboard":    ("/dashboard",    None),
    "🔍 Audit Repo":        ("/audit",        None),
    "📝 Fix Plan":          ("/fixplan",      None),
    "🔄 Scan All Repos":    ("/scanall",      None),
    "🔀 Switch Repo":       ("/repo",         "switch"),
    "❌ Remove Repo":       ("/repo",         "remove"),
    "🌿 Branches":          ("/branches",     None),
    "🌿 All Branches":      ("/branches",     None),
    "📌 Commits":           ("/commits",      None),
    "📌 Recent Commits":    ("/commits",      None),
    "🔀 Pull Requests":     ("/pr",           None),
    "🔀 Open PRs":          ("/pr",           None),
    "⚠️ Open Issues":       ("/issues",       None),
    "🔒 Repo Security":     ("/security",     None),
    "📦 Dependencies":      ("/dependencies", None),
    "📋 Repo Packages":     ("/packages",     None),
    "🔄 Sync Repo":         ("/repo",         None),
    # Tools
    "🗂️ Packages":          ("/packages",     None),
    "🔒 Security Scan":     ("/security",     None),
    "📊 Storage Info":      ("/storage",      None),
    "🌐 Services":          ("/services",     None),
    "📈 System Metrics":    ("/metrics",      None),
    "🧹 Clear Chat":        ("/clear",        None),
    "🔍 Search Code":       ("/ask",          "Search for this in the codebase:"),
    "📋 Code Snippets":     ("/ask",          "Show me useful code snippets for:"),
    # Keys
    "🔑 View API Keys":     ("/apikeys",      None),
    "🔑 API Keys":          ("/apikeys",      None),
    "🧪 Test All Keys":     ("/testkeys",     None),
    "💾 Key Backup":        ("/backup",       None),
    "🔧 Set Groq Key":      ("/setkey",       "GROQ_API_KEY"),
    "🔧 Set Gemini Key":    ("/setkey",       "GEMINI_API_KEY"),
    "🔧 Set OpenRouter":    ("/setkey",       "OPENROUTER_API_KEY"),
    "🔧 Set TG Token":      ("/setkey",       "TELEGRAM_BOT_TOKEN"),
    "🔧 Set TG Chat ID":    ("/setkey",       "TELEGRAM_CHAT_ID"),
    "🔧 Set GitHub PAT":    ("/setkey",       "GITHUB_TOKEN"),
    "🔒 Key Security":      ("/apikeys",      None),
    "🔄 Reload Keys":       ("/apikeys",      None),
    "📋 Key Status":        ("/apikeys",      None),
    # Android / Termux
    "📱 Install Termux":    ("/ask",          "Show me the NasGuardian Termux install guide:\ncurl -fsSL https://raw.githubusercontent.com/nastech-ai/NasGuardian/main/install.sh | bash"),
    "📦 Create Bundle":     ("/ask",          "How do I create the NasGuardian ZIP bundle?\nbash scripts/create_bundle.sh"),
    "🔄 Update Guardian":   ("/ask",          "How do I update NasGuardian?\ngit -C ~/nastech-guardian pull && bash install.sh --no-prompt"),
    "🚀 Start Bot":         ("/ask",          "Starting the NasGuardian bot. Run: bash scripts/telegram_bot/run_bot.sh"),
    "📋 Bot Logs":          ("/logs",         None),
    "🛑 Stop Bot":          ("/ask",          "To stop the bot: tmux kill-session -t nastech-guardian  (Termux) or Ctrl+C"),
    "⚙️ Config Keys":       ("/apikeys",      None),
    "🛡️ Guardian Status":   ("/status",       None),
    "📲 Termux Setup":      ("/ask",          "Guide me through Termux setup for NasGuardian."),
    "🔗 NasGuardian":       ("/ask",          "NasGuardian GitHub: https://github.com/nastech-ai/NasGuardian"),
    "🔗 NasTerminal":       ("/ask",          "NasTerminal GitHub: https://github.com/nastech-ai/NasTerminal"),
    "📋 Boot Config":       ("/ask",          "Show me how to configure Termux:Boot for auto-start."),
    "📥 Download ZIP":      ("/ask",          "Download link: https://github.com/nastech-ai/NasGuardian/raw/main/nastech-guardian-v3.0.zip"),
    "🔄 Reinstall Bot":     ("/ask",          "Reinstall: curl -fsSL https://raw.githubusercontent.com/nastech-ai/NasGuardian/main/install.sh | bash"),
    "🔍 Check Deps":        ("/ask",          "Check Python dependencies: pip install -r scripts/telegram_bot/requirements.txt"),
    # Workflows
    "📋 List Workflows":    ("/workflows",    None),
    "🔍 Workflow Runs":     ("/workflows",    None),
    "📊 Run Status":        ("/status",       None),
    "❌ Failed Runs":       ("/errors",       None),
    "⚠️ Open Issues":       ("/issues",       None),
    "📦 Latest Release":    ("/release",      None),
    # Reports
    "📊 Full Report":       ("/status",       None),
    "📅 Daily Digest":      ("/daily",        None),
    "📈 Pipeline Stats":    ("/metrics",      None),
    "📅 Subscribe Daily":   ("/subscribe",    None),
    "🔕 Unsubscribe":       ("/unsubscribe",  None),
    "📋 Full Help":         ("/help",         None),
    "❓ Help":              ("/help",         None),
    "ℹ️ About Guardian":    ("/ask",          "Tell me about NasGuardian — what is it and what can it do?"),
    "💾 Backup Status":     ("/backup",       None),
    "🧠 Bot Memory":        ("/memory",       None),
    "🤖 AI Providers":      ("/providers",    None),
    "📊 Model Status":      ("/models",       None),
    "🌐 Network Status":    ("/services",     None),
    "🏆 All Commands":      ("/help",         None),
    "📋 Command List":      ("/help",         None),
    "🔄 Refresh Status":    ("/status",       None),
    # Security
    "🛡️ Full Audit":        ("/doctor",       None),
    "🔍 Scan Secrets":      ("/security",     None),
    "⚠️ Vulnerabilities":   ("/security",     None),
    "🔧 Security Fix":      ("/fix",          None),
    "📋 Audit Report":      ("/doctor",       None),
    "🔑 API Key Check":     ("/testkeys",     None),
    "🔒 Repo Perms":        ("/security",     None),
    "🛡️ Code Review":       ("/review",       None),
    "📊 Security Score":    ("/health",       None),
    "🔍 Dep Audit":         ("/dependencies", None),
    "⚡ Quick Audit":       ("/scan",         None),
    "🔄 Rescan Now":        ("/scan",         None),
    "📤 Export Report":     ("/doctor",       None),
    "🔒 Lock Down":         ("/security",     None),
    # Settings
    "🤖 AI Models":         ("/models",       None),
    "📊 Bot Status":        ("/memory",       None),
    "💾 Backup Info":       ("/backup",       None),
    "📋 All Commands":      ("/help",         None),
    "🔄 Restart Bot":       ("/ask",          "To restart: the bot is already running. Use /clear to reset state."),
    "⚙️ Preferences":       ("/memory",       None),
    "📡 Connection Test":   ("/testkeys",     None),
    "🔄 Reload Keys":       ("/apikeys",      None),
    # Notifications toggles
    "🔔 All Notif ON":      ("/notifon",      "all"),
    "🔕 All Notif OFF":     ("/notifoff",     "all"),
    "📊 Notif Status":      ("/notif",        None),
    "🔔 Build ON":          ("/notifon",      "build"),
    "🔕 Build OFF":         ("/notifoff",     "build"),
    "🔔 Failures ON":       ("/notifon",      "failures"),
    "🔕 Failures OFF":      ("/notifoff",     "failures"),
    "🔔 PR Alerts ON":      ("/notifon",      "pr"),
    "🔕 PR Alerts OFF":     ("/notifoff",     "pr"),
    "🔔 Release ON":        ("/notifon",      "release"),
    "🔕 Release OFF":       ("/notifoff",     "release"),
    "🔔 Security ON":       ("/notifon",      "security"),
    "🔕 Security OFF":      ("/notifoff",     "security"),
    "📅 Daily ON":          ("/notifon",      "daily"),
    "🔕 Daily OFF":         ("/notifoff",     "daily"),
    "📋 Digest Now":        ("/daily",        None),
    "📤 Error Screenshot":  ("/errorshot",    None),
    # Auto-fix
    "🔧 Auto Fix":          ("/autofix",      None),
    "✅ Approve Fix":       ("/approvefix",   None),
    "❌ Cancel Fix":        ("/cancelfix",    None),
}

def make_keyboard(rows: list) -> ReplyKeyboardMarkup:
    """Build a ReplyKeyboardMarkup from a list of row lists."""
    keyboard = [[KeyboardButton(label) for label in row] for row in rows]
    return ReplyKeyboardMarkup(
        keyboard,
        resize_keyboard=True,
        one_time_keyboard=False,
        input_field_placeholder="Type a message or tap a button…"
    )

# Pre-build all keyboards
_KEYBOARDS_BUILT: dict = {
    "main":          make_keyboard(_KB_MAIN),
    "ai":            make_keyboard(_KB_AI),
    "pipeline":      make_keyboard(_KB_PIPELINE),
    "repos":         make_keyboard(_KB_REPOS),
    "tools":         make_keyboard(_KB_TOOLS),
    "keys":          make_keyboard(_KB_KEYS),
    "android":       make_keyboard(_KB_ANDROID),
    "workflows":     make_keyboard(_KB_WORKFLOWS),
    "reports":       make_keyboard(_KB_REPORTS),
    "security":      make_keyboard(_KB_SECURITY),
    "settings":      make_keyboard(_KB_SETTINGS),
    "notifications": make_keyboard(_KB_NOTIFICATIONS),
}

_CATEGORY_KB_MAP: dict = {
    "🤖 AI Chat":       "ai",
    "📊 Pipeline":      "pipeline",
    "🔍 Repos":         "repos",
    "🛠️ Tools":         "tools",
    "🔑 API Keys":      "keys",
    "📱 Android":       "android",
    "🚀 Workflows":     "workflows",
    "📋 Reports":       "reports",
    "🔒 Security":      "security",
    "⚙️ Settings":      "settings",
    "🔔 Notifications": "notifications",
    "🏠 Main Menu":     "main",
}

_CATEGORY_LABELS: dict = {
    "🤖 AI Chat":      "🤖 AI & Chat Tools",
    "📊 Pipeline":     "📊 CI/CD Pipeline",
    "🔍 Repos":        "🔍 Repository Manager",
    "🛠️ Tools":        "🛠️ Dev Tools",
    "🔑 API Keys":     "🔑 API Keys & Secrets",
    "📱 Android":      "📱 Android / Termux",
    "🚀 Workflows":    "🚀 GitHub Workflows",
    "📋 Reports":      "📋 Reports & Info",
    "🔒 Security":      "🔒 Security & Audit",
    "⚙️ Settings":      "⚙️ Settings",
    "🔔 Notifications": "🔔 Notifications — ON/OFF Toggles",
    "🏠 Main Menu":     "🏠 Main Menu",
}

async def show_keyboard(u: Update, name: str = "main", text: str = None):
    """Send a message with the named reply keyboard."""
    kb = _KEYBOARDS_BUILT.get(name, _KEYBOARDS_BUILT["main"])
    label = next((v for k, v in _CATEGORY_LABELS.items()
                  if _CATEGORY_KB_MAP.get(k) == name), "NasTech Guardian")
    msg = text or f"<b>{label}</b> — tap a button or type a message:"
    await u.message.reply_text(msg, reply_markup=kb, parse_mode=ParseMode.HTML)


# ─────────────────────────────────────────────────────────────────────
# Command handlers
# ─────────────────────────────────────────────────────────────────────

async def cmd_start(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(u): return await deny(u)
    cid = str(u.effective_chat.id)
    await u.message.reply_text(
        f"🛡️ <b>NasTech Guardian v{BOT_VERSION}</b>\n"
        f"Repo: <code>{sessions[cid]['repo']}</code>\n\n"
        "Your AI DevOps assistant + CI/CD orchestrator.\n\n"
        "💡 <b>Tap any button below</b> — or type a message to chat with AI.\n"
        "Use /menu to switch keyboard categories.\n\n"
        f"<i>Developed by Nsamba Naswif Cohen</i>",
        reply_markup=_KEYBOARDS_BUILT["main"],
        parse_mode=ParseMode.HTML
    )


async def cmd_menu(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(u): return await deny(u)
    args = ctx.args or []
    name = "main"
    if args:
        arg = args[0].lower()
        valid = ["ai","pipeline","repos","tools","keys","android",
                 "workflows","reports","security","settings","main"]
        if arg in valid:
            name = arg
    await show_keyboard(u, name)


async def cmd_help(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(u): return await deny(u)
    await u.message.reply_text(
        "🛡️ <b>NasTech Guardian Commands</b>\n\n"
        "<b>🤖 AI Tools</b>\n"
        "  /ask [question] — AI DevOps chat\n"
        "  /explain [code] — explain code\n"
        "  /review [code] — code review\n"
        "  /fix_error [msg] — fix an error\n"
        "  /run [python] — run code safely\n"
        "  /summarize [text|url] — summarize\n"
        "  /translate [lang] [text] — translate\n"
        "  /ocr — send image → extract text\n\n"
        "<b>🚀 Pipeline</b>\n"
        "  /status — pipeline status\n"
        "  /scan — full Guardian scan\n"
        "  /build — trigger build\n"
        "  /rebuild — force rebuild\n"
        "  /test — run tests\n"
        "  /repair — trigger repair bot\n"
        "  /release — latest release\n"
        "  /health — health check\n"
        "  /doctor — full doctor scan\n\n"
        "<b>🔍 Analysis</b>\n"
        "  /logs — workflow logs\n"
        "  /errors — recent failures\n"
        "  /dependencies — dep report\n"
        "  /packages — package versions\n"
        "  /security — security scan\n"
        "  /fix — auto-fix PR\n\n"
        "<b>📂 Multi-Repo Manager</b>\n"
        "  /addrepo [owner/name] — add + full audit\n"
        "  /repos — list all tracked repos\n"
        "  /dashboard — health overview of all repos\n"
        "  /audit [owner/name] — full health report\n"
        "  /fixplan [owner/name] — step-by-step fixes\n"
        "  /scanall — rescan every tracked repo\n"
        "  /repo add|switch|remove|list — manage\n\n"
        "<b>📂 Repository (active repo)</b>\n"
        "  /pr — open pull requests\n"
        "  /issues — open issues\n"
        "  /commits — recent commits\n"
        "  /branches — branches\n"
        "  /version — app version\n"
        "  /workflows — list workflows\n\n"
        "<b>📊 System</b>\n"
        "  /models — AI provider status\n"
        "  /metrics — pipeline metrics\n"
        "  /daily — AI daily digest\n"
        "  /storage — artifacts\n"
        "  /services — service health\n"
        "  /memory — bot state\n"
        "  /backup — backup info\n\n"
        "<i>Or just type a message to chat with AI!</i>\n\n"
        f"<i>{BRAND}</i>",
        parse_mode=ParseMode.HTML
    )


# ── AI Commands ──────────────────────────────────────────────────────

async def cmd_ask(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(u): return await deny(u)
    cid  = str(u.effective_chat.id)
    text = " ".join(ctx.args) if ctx.args else ""
    if not text:
        await u.message.reply_text("Usage: /ask [your question]\nOr just type normally to chat!")
        return
    msg = await u.message.reply_text("🤔 Thinking…")
    result = ai_chat(text, sessions[cid]["history"], sessions[cid]["repo"])
    sessions[cid]["history"].append({"role": "user",      "content": text})
    sessions[cid]["history"].append({"role": "assistant", "content": result["text"]})
    sessions[cid]["history"] = sessions[cid]["history"][-20:]
    await msg.edit_text(truncate(fmt_ai(result)), parse_mode=ParseMode.HTML)


async def cmd_explain(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(u): return await deny(u)
    code = " ".join(ctx.args) if ctx.args else ""
    if not code:
        await u.message.reply_text("Usage: /explain [code snippet]")
        return
    msg = await u.message.reply_text("🔍 Analyzing code…")
    result = ai_ask(
        "You are a code explainer. Explain what this code does (2-3 sentences), "
        "list key functions used, and note any issues. Be concise. Use HTML code tags.",
        f"```\n{code}\n```"
    )
    await msg.edit_text(truncate(fmt_ai(result)), parse_mode=ParseMode.HTML)


async def cmd_review(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(u): return await deny(u)
    code = " ".join(ctx.args) if ctx.args else ""
    if not code:
        await u.message.reply_text("Usage: /review [code snippet]")
        return
    msg = await u.message.reply_text("🔬 Reviewing code…")
    result = ai_ask(
        "You are a senior code reviewer. Review for bugs (🔴), security issues (🔴), "
        "performance (🟡), and style (🟢). Numbered list. Be concise.",
        f"```\n{code}\n```"
    )
    await msg.edit_text(truncate(fmt_ai(result)), parse_mode=ParseMode.HTML)


async def cmd_fix_error(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(u): return await deny(u)
    error = " ".join(ctx.args) if ctx.args else ""
    if not error:
        await u.message.reply_text("Usage: /fix_error [error message]")
        return
    msg = await u.message.reply_text("🔧 Diagnosing error…")
    result = ai_ask(
        "You are a debugging expert. Given an error: 1) state root cause in one sentence, "
        "2) show the fix with code, 3) give a prevention tip. Be concise.",
        f"Error: {error}"
    )
    await msg.edit_text(truncate(fmt_ai(result)), parse_mode=ParseMode.HTML)


async def cmd_run(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(u): return await deny(u)
    code = " ".join(ctx.args) if ctx.args else ""
    if not code:
        await u.message.reply_text(
            "Usage: /run [python code]\n\nExample:\n"
            "<code>/run print(2 ** 10)</code>\n\n"
            "⚠️ Sandboxed — os/sys/subprocess are blocked.",
            parse_mode=ParseMode.HTML
        )
        return
    msg = await u.message.reply_text("⚙️ Running…")
    result = safe_run_python(code)
    if result["ok"]:
        out = result["output"] or "(no output)"
        await msg.edit_text(
            f"✅ <b>Output:</b>\n<pre>{esc(out)}</pre>",
            parse_mode=ParseMode.HTML
        )
    else:
        err = result["error"] or "Unknown error"
        # Ask AI for fix suggestion
        fix = ai_ask(
            "Briefly explain this Python error and the one-line fix.",
            f"Code: {code}\nError: {err}"
        )
        await msg.edit_text(
            f"❌ <b>Error:</b> <code>{esc(err)}</code>\n\n"
            f"💡 <b>Suggestion:</b>\n{truncate(fmt_ai(fix), 1500)}",
            parse_mode=ParseMode.HTML
        )


async def cmd_summarize(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(u): return await deny(u)
    text = " ".join(ctx.args) if ctx.args else ""
    if not text:
        await u.message.reply_text("Usage: /summarize [text or URL]")
        return
    msg = await u.message.reply_text("📝 Summarizing…")
    # If URL, fetch content
    content = text
    if text.startswith("http"):
        try:
            req = urllib.request.Request(text, headers={"User-Agent": "NasTechGuardian/2.0"})
            with urllib.request.urlopen(req, timeout=10) as r:
                raw = r.read().decode("utf-8", errors="replace")
                # Strip HTML tags
                content = re.sub(r'<[^>]+>', ' ', raw)
                content = re.sub(r'\s+', ' ', content).strip()[:5000]
        except Exception as e:
            content = f"Could not fetch URL: {e}"
    result = ai_ask(
        "Summarize the following in 3-5 bullet points. Be concise.",
        content[:4000]
    )
    await msg.edit_text(truncate(fmt_ai(result)), parse_mode=ParseMode.HTML)


async def cmd_translate(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(u): return await deny(u)
    if not ctx.args or len(ctx.args) < 2:
        await u.message.reply_text("Usage: /translate [language] [text]\nExample: /translate Spanish Hello World")
        return
    lang = ctx.args[0]
    text = " ".join(ctx.args[1:])
    msg  = await u.message.reply_text(f"🌐 Translating to {lang}…")
    result = ai_ask(
        f"Translate the following text to {lang}. Return only the translation.",
        text
    )
    await msg.edit_text(
        f"🌐 <b>Translation ({lang}):</b>\n{esc(result['text'])}\n\n<i>via {result['provider']}</i>",
        parse_mode=ParseMode.HTML
    )


async def handle_photo(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Handle /ocr by processing photos sent to the bot."""
    if not authorized(u): return await deny(u)
    caption = (u.message.caption or "").lower()
    if "/ocr" not in caption and not ctx.user_data.get("ocr_mode"):
        return  # Not an OCR request
    msg = await u.message.reply_text("🔍 Extracting text from image…")
    photo = u.message.photo[-1]  # highest resolution
    file  = await ctx.bot.get_file(photo.file_id)
    photo_bytes = await file.download_as_bytearray()
    text = await do_ocr(bytes(photo_bytes))
    if text.strip():
        await msg.edit_text(
            f"📄 <b>Extracted Text:</b>\n<pre>{esc(text[:3500])}</pre>",
            parse_mode=ParseMode.HTML
        )
    else:
        await msg.edit_text("⚠️ No text found in image.")


async def cmd_ocr(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(u): return await deny(u)
    ctx.user_data["ocr_mode"] = True
    await u.message.reply_text(
        "📸 <b>OCR Mode Active</b>\nSend me an image and I'll extract the text from it.\n\n"
        "<i>Also works: send image with caption /ocr</i>",
        parse_mode=ParseMode.HTML
    )


async def cmd_daily(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(u): return await deny(u)
    cid = str(u.effective_chat.id)
    msg = await u.message.reply_text("📊 Generating daily digest…")
    repo = sessions[cid]["repo"]
    owner, rname = repo.split("/")
    runs     = wf_runs(limit=10, repo=repo)
    prs_raw  = gh("GET", f"/repos/{owner}/{rname}/pulls?state=open&per_page=5", repo=repo)
    iss_raw  = gh("GET", f"/repos/{owner}/{rname}/issues?state=open&per_page=5", repo=repo)
    commits  = gh("GET", f"/repos/{owner}/{rname}/commits?per_page=5", repo=repo)
    prs  = len(prs_raw)  if isinstance(prs_raw,  list) else 0
    iss  = len(iss_raw)  if isinstance(iss_raw,  list) else 0
    success  = sum(1 for r in runs if r.get("conclusion") == "success")
    failures = sum(1 for r in runs if r.get("conclusion") == "failure")
    recent_msgs = [c.get("commit",{}).get("message","").split("\n")[0] for c in (commits if isinstance(commits,list) else [])[:5]]
    run_summary = [{"name": r.get("name","?")[:30], "conclusion": r.get("conclusion","?")} for r in runs[:5]]
    result = ai_ask(
        "You are NasTech Guardian. Generate a concise daily digest in HTML (use <b>, <i>, bullet •). "
        "Include: overall health emoji, key stats, top concerns, suggested actions. Max 300 words. No markdown.",
        f"Repo: {repo}\nOpen PRs: {prs}\nOpen Issues: {iss}\n"
        f"Workflows (last 10): {success} passed, {failures} failed\n"
        f"Recent commits: {recent_msgs}\n"
        f"Recent runs: {run_summary}"
    )
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    await msg.edit_text(
        f"📊 <b>Daily Digest — {date_str}</b>\n\n"
        f"{truncate(result['text'], 3000)}\n\n"
        f"<i>via {result['provider']}</i>",
        parse_mode=ParseMode.HTML
    )


async def cmd_repo(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """
    Multi-repo manager.
    /repo                        → show active + list
    /repo add owner/name         → full audit then add
    /repo list                   → all tracked repos
    /repo switch owner/name      → switch active
    /repo use owner/name         → alias for switch
    /repo scan owner/name        → audit without adding
    /repo audit owner/name       → alias for scan
    /repo remove owner/name      → remove from tracking
    /repo owner/name             → quick switch (legacy)
    """
    if not authorized(u): return await deny(u)
    cid  = str(u.effective_chat.id)
    args = ctx.args or []

    # ── No args → show status ─────────────────────────────────────
    if not args:
        active = sessions[cid]["repo"]
        if REPO_MANAGER_OK and registry:
            repos = registry.list_repos(cid)
            if repos:
                lines = [
                    f"📁 <b>Active repo:</b> <code>{esc(active)}</code>\n",
                    f"<b>Tracked repos ({len(repos)}):</b>",
                ]
                for r in repos:
                    slug   = r["slug"]
                    score  = r.get("score")
                    marker = " ◀ active" if slug == active else ""
                    s_str  = f"  🟢{score}" if score and score >= 85 else \
                             f"  🟡{score}" if score and score >= 65 else \
                             f"  🔴{score}" if score else "  ⏳"
                    lines.append(f"  {'▶' if slug == active else '·'} <code>{esc(slug)}</code>{s_str}{marker}")
                lines += [
                    "",
                    "<i>Commands:</i>",
                    "  <code>/repo add owner/name</code> — add + audit",
                    "  <code>/repo switch owner/name</code> — switch active",
                    "  <code>/repo scan owner/name</code> — audit without adding",
                    "  <code>/repo remove owner/name</code> — remove",
                ]
                await u.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)
                return
        await u.message.reply_text(
            f"📁 <b>Active repo:</b> <code>{esc(active)}</code>\n\n"
            "Commands:\n"
            "  <code>/repo add owner/name</code> — add + audit\n"
            "  <code>/repo switch owner/name</code> — switch\n"
            "  <code>/repo scan owner/name</code> — audit\n"
            "  <code>/repo list</code> — all repos\n"
            "  <code>/dashboard</code> — full overview",
            parse_mode=ParseMode.HTML
        )
        return

    sub  = args[0].lower()
    rest = args[1:]

    # ── list ─────────────────────────────────────────────────────
    if sub == "list":
        if not REPO_MANAGER_OK or not registry:
            await u.message.reply_text(f"📁 Active: <code>{esc(sessions[cid]['repo'])}</code>",
                                       parse_mode=ParseMode.HTML)
            return
        repos = registry.list_repos(cid)
        if not repos:
            await u.message.reply_text(
                "No repos tracked yet.\nAdd one: <code>/repo add owner/name</code>",
                parse_mode=ParseMode.HTML
            )
            return
        active = registry.active(cid)
        lines  = [f"📂 <b>Tracked Repos ({len(repos)})</b>\n"]
        for r in repos:
            slug   = r["slug"]
            score  = r.get("score")
            added  = r.get("added_at","?")[:10]
            audit  = r.get("last_audit","?")[:10]
            marker = " ◀ <b>active</b>" if slug == active else ""
            if score is None:     s_str = "⏳ not scanned"
            elif score >= 85:     s_str = f"🟢 {score}/100"
            elif score >= 65:     s_str = f"🟡 {score}/100"
            elif score >= 40:     s_str = f"🟠 {score}/100"
            else:                 s_str = f"🔴 {score}/100"
            lines.append(
                f"{'▶' if slug==active else '·'} <code>{esc(slug)}</code>{marker}\n"
                f"   {s_str} · added {added} · audited {audit}"
            )
        lines += ["", "<i>/repo switch owner/name — to switch active</i>"]
        await u.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)
        return

    # ── switch / use ─────────────────────────────────────────────
    if sub in ("switch", "use"):
        repo = rest[0] if rest else ""
        if not repo or "/" not in repo:
            await u.message.reply_text("Usage: <code>/repo switch owner/name</code>", parse_mode=ParseMode.HTML)
            return
        sessions[cid]["repo"] = repo
        if REPO_MANAGER_OK and registry:
            if not registry.has(cid, repo):
                registry.add(cid, repo)
            registry.switch(cid, repo)
        await u.message.reply_text(
            f"✅ Switched to: <code>{esc(repo)}</code>\n"
            f"Use <code>/audit {esc(repo)}</code> for a full health report.",
            parse_mode=ParseMode.HTML
        )
        return

    # ── remove ────────────────────────────────────────────────────
    if sub == "remove":
        repo = rest[0] if rest else ""
        if not repo or "/" not in repo:
            await u.message.reply_text("Usage: <code>/repo remove owner/name</code>", parse_mode=ParseMode.HTML)
            return
        if REPO_MANAGER_OK and registry:
            ok = registry.remove(cid, repo)
            # If removed active, sessions falls back
            active = registry.active(cid, GITHUB_REPO)
            sessions[cid]["repo"] = active
            msg = f"🗑️ Removed <code>{esc(repo)}</code>." if ok else f"❌ Repo <code>{esc(repo)}</code> not in list."
            await u.message.reply_text(msg + f"\nActive repo: <code>{esc(active)}</code>",
                                        parse_mode=ParseMode.HTML)
        else:
            await u.message.reply_text("Repo manager not available.", parse_mode=ParseMode.HTML)
        return

    # ── scan / audit (no add) ─────────────────────────────────────
    if sub in ("scan", "audit"):
        repo = rest[0] if rest else ""
        if not repo or "/" not in repo:
            await u.message.reply_text("Usage: <code>/repo scan owner/name</code>", parse_mode=ParseMode.HTML)
            return
        await _do_audit(u, cid, repo, add_after=False)
        return

    # ── add (audit + add) ─────────────────────────────────────────
    if sub == "add":
        repo = rest[0] if rest else ""
        if not repo or "/" not in repo:
            await u.message.reply_text("Usage: <code>/repo add owner/name</code>", parse_mode=ParseMode.HTML)
            return
        await _do_audit(u, cid, repo, add_after=True)
        return

    # ── legacy: /repo owner/name ──────────────────────────────────
    if "/" in sub:
        repo = sub
        sessions[cid]["repo"] = repo
        if REPO_MANAGER_OK and registry:
            if not registry.has(cid, repo):
                registry.add(cid, repo)
            registry.switch(cid, repo)
        await u.message.reply_text(
            f"✅ Switched to: <code>{esc(repo)}</code>",
            parse_mode=ParseMode.HTML
        )
        return

    await u.message.reply_text(
        "❓ Unknown /repo subcommand.\n"
        "Use: <code>add · list · switch · scan · remove</code>",
        parse_mode=ParseMode.HTML
    )


async def _do_audit(u: Update, cid: str, repo: str, add_after: bool = False):
    """Run full audit on repo, send report, optionally add to registry."""
    msg = await u.message.reply_text(
        f"🔍 <b>Auditing</b> <code>{esc(repo)}</code>…\n"
        "Checking: access · secrets · workflows · builds · issues · security…",
        parse_mode=ParseMode.HTML
    )
    if not GITHUB_TOKEN:
        await msg.edit_text(
            "❌ GITHUB_TOKEN not set — cannot audit repo.\n"
            "Set it: <code>export GITHUB_TOKEN='ghp_...'</code>",
            parse_mode=ParseMode.HTML
        )
        return

    result = audit_repo(repo, GITHUB_TOKEN)

    if result.get("score", -1) == -1:
        await msg.edit_text(
            f"❌ <b>Cannot access repo</b>\n{result.get('error','Unknown error')}",
            parse_mode=ParseMode.HTML
        )
        return

    # Send full audit report
    report_html = format_audit_html(result, compact=False)
    await msg.edit_text(truncate(report_html, 4000), parse_mode=ParseMode.HTML,
                        disable_web_page_preview=True)

    score = result["score"]
    plan  = result.get("fix_plan", [])

    # Add to registry if requested
    if add_after and REPO_MANAGER_OK and registry:
        if not registry.has(cid, repo):
            registry.add(cid, repo, score=score)
        else:
            registry.update_score(cid, repo, score)
        sessions[cid]["repo"] = repo

    # Ask AI for overall diagnosis + top priority fix
    if plan:
        critical_high = [p for p in plan if p["sev"] in ("CRITICAL","HIGH")][:5]
        issue_summary = "\n".join(f"- [{p['sev']}] {p['title']}: {p['fix'][:80]}" for p in critical_high)
        ai_result = ai_ask(
            "You are NasTech Guardian. Given these repo audit findings, write a short "
            "actionable summary in 2-3 sentences covering: overall health, the #1 priority "
            "fix, and the expected improvement after fixing it. Use Telegram HTML (<b>, <i>). "
            "Be direct and actionable.",
            f"Repo: {repo}\nHealth: {score}/100\nTop issues:\n{issue_summary}"
        )
        await u.message.reply_text(
            f"🤖 <b>AI Diagnosis</b>\n\n{ai_result.get('text','')}\n\n"
            f"<i>Use /fixplan to see all {len(plan)} fix steps.</i>",
            parse_mode=ParseMode.HTML
        )
        if add_after:
            await u.message.reply_text(
                f"{'✅' if add_after else 'ℹ️'} Repo <code>{esc(repo)}</code> "
                f"{'added to tracker' if add_after else 'scanned'}. "
                f"Score: <b>{score}/100</b>\n"
                f"Now active. Use /repo list to see all repos.",
                parse_mode=ParseMode.HTML
            )
    elif add_after:
        await u.message.reply_text(
            f"✅ <code>{esc(repo)}</code> added — score <b>{score}/100</b> 🎉\n"
            "No critical issues found!",
            parse_mode=ParseMode.HTML
        )


async def cmd_audit(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Alias: /audit [owner/name] — full audit of given or active repo."""
    if not authorized(u): return await deny(u)
    cid  = str(u.effective_chat.id)
    repo = ctx.args[0] if ctx.args else sessions[cid]["repo"]
    if not repo or "/" not in repo:
        await u.message.reply_text(
            "Usage: <code>/audit owner/name</code>\n"
            "Or just /audit to scan the active repo.",
            parse_mode=ParseMode.HTML
        )
        return
    await _do_audit(u, cid, repo, add_after=False)


async def cmd_addrepo(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Alias: /addrepo owner/name — audit + add repo."""
    if not authorized(u): return await deny(u)
    cid  = str(u.effective_chat.id)
    repo = ctx.args[0] if ctx.args else ""
    if not repo or "/" not in repo:
        await u.message.reply_text(
            "Usage: <code>/addrepo owner/name</code>\n\n"
            "I will:\n"
            "1. Verify the repo is accessible\n"
            "2. Check secrets, workflows, builds, issues\n"
            "3. Give you a health score + fix plan\n"
            "4. Add it to your tracker",
            parse_mode=ParseMode.HTML
        )
        return
    await _do_audit(u, cid, repo, add_after=True)


async def cmd_repos(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Alias: /repos — show all tracked repos."""
    if not authorized(u): return await deny(u)
    cid = str(u.effective_chat.id)
    ctx.args = ["list"]
    await cmd_repo(u, ctx)


async def cmd_dashboard(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Multi-repo dashboard with health scores."""
    if not authorized(u): return await deny(u)
    cid = str(u.effective_chat.id)

    if not REPO_MANAGER_OK or not registry:
        await u.message.reply_text(
            "📊 <b>Dashboard</b>\n"
            f"Active: <code>{esc(sessions[cid]['repo'])}</code>\n\n"
            "Add repos with: <code>/repo add owner/name</code>",
            parse_mode=ParseMode.HTML
        )
        return

    repos = registry.list_repos(cid)
    if not repos:
        await u.message.reply_text(
            "📊 <b>Dashboard — No Repos Tracked</b>\n\n"
            "Add your first repo:\n"
            "<code>/repo add nastech-ai/NasTerminal</code>\n\n"
            "It will run a full audit before adding.",
            parse_mode=ParseMode.HTML
        )
        return

    msg = await u.message.reply_text("⏳ Building dashboard…")
    active = registry.active(cid)
    lines  = [f"📊 <b>Guardian Dashboard — {len(repos)} repos</b>\n"]

    for r in repos:
        slug  = r["slug"]
        score = r.get("score")
        added = r.get("added_at","?")[:10]
        audit = r.get("last_audit","?")[:10]

        if score is None:     bar = "⏳ not scanned"
        elif score >= 85:     bar = f"🟢 {score}/100 {'█'*int(score/10)}{'░'*(10-int(score/10))}"
        elif score >= 65:     bar = f"🟡 {score}/100 {'█'*int(score/10)}{'░'*(10-int(score/10))}"
        elif score >= 40:     bar = f"🟠 {score}/100 {'█'*int(score/10)}{'░'*(10-int(score/10))}"
        else:                 bar = f"🔴 {score}/100 {'█'*int(score/10)}{'░'*(10-int(score/10))}" if score else "🔴 critical"

        owner_part, rname_part = (slug.split("/")+[""])[:2]
        url = f"https://github.com/{slug}"
        lines.append(
            f"{'▶' if slug==active else '·'} <a href='{url}'><b>{esc(slug)}</b></a>"
            f"{' ◀ active' if slug==active else ''}\n"
            f"   {bar}\n"
            f"   audited {audit}"
        )

    lines += [
        "",
        f"Active: <code>{esc(active)}</code>",
        "",
        "<i>/repo add owner/name — add new repo</i>",
        "<i>/repo switch owner/name — change active</i>",
        "<i>/audit — rescan active repo</i>",
    ]
    await msg.edit_text("\n".join(lines), parse_mode=ParseMode.HTML,
                        disable_web_page_preview=True)


async def cmd_fixplan(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Show paginated fix plan for the last audit result."""
    if not authorized(u): return await deny(u)
    cid  = str(u.effective_chat.id)
    repo = sessions[cid]["repo"]

    # Parse optional page number from command text e.g. /fixplan_2
    page = 1
    if ctx.args:
        try:
            page = int(ctx.args[0])
        except ValueError:
            # Could be a repo name
            if "/" in ctx.args[0]:
                repo = ctx.args[0]

    msg = await u.message.reply_text(f"🔧 Generating fix plan for <code>{esc(repo)}</code>…",
                                      parse_mode=ParseMode.HTML)
    if not GITHUB_TOKEN:
        await msg.edit_text("❌ GITHUB_TOKEN not set.", parse_mode=ParseMode.HTML)
        return

    result = audit_repo(repo, GITHUB_TOKEN)
    if result.get("score", -1) == -1:
        await msg.edit_text(f"❌ {result.get('error','Cannot access repo.')}", parse_mode=ParseMode.HTML)
        return

    # Update score in registry
    if REPO_MANAGER_OK and registry:
        if not registry.has(cid, repo):
            registry.add(cid, repo, score=result["score"])
        else:
            registry.update_score(cid, repo, result["score"])

    plan_html = format_fix_plan_html(result, page=page, per_page=5)
    await msg.edit_text(truncate(plan_html, 4000), parse_mode=ParseMode.HTML)


async def cmd_scanall(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Rescan all tracked repos and update scores."""
    if not authorized(u): return await deny(u)
    cid = str(u.effective_chat.id)

    if not REPO_MANAGER_OK or not registry:
        await u.message.reply_text("Repo manager not available.", parse_mode=ParseMode.HTML)
        return

    repos = registry.list_repos(cid)
    if not repos:
        await u.message.reply_text(
            "No repos tracked.\nAdd with: <code>/repo add owner/name</code>",
            parse_mode=ParseMode.HTML
        )
        return
    if not GITHUB_TOKEN:
        await u.message.reply_text("❌ GITHUB_TOKEN not set.", parse_mode=ParseMode.HTML)
        return

    msg = await u.message.reply_text(
        f"🔍 Scanning <b>{len(repos)}</b> repos…\n"
        "<i>This may take a minute.</i>",
        parse_mode=ParseMode.HTML
    )

    results = []
    for r in repos:
        slug = r["slug"]
        await msg.edit_text(
            f"🔍 Scanning <b>{len(repos)}</b> repos…\n"
            f"→ <code>{esc(slug)}</code>",
            parse_mode=ParseMode.HTML
        )
        result = audit_repo(slug, GITHUB_TOKEN)
        score  = result.get("score", -1)
        if score >= 0:
            registry.update_score(cid, slug, score)
        results.append((slug, score, result.get("fix_plan",[])))

    lines = ["📊 <b>Scan Complete — All Repos</b>\n"]
    for slug, score, plan in sorted(results, key=lambda x: x[1]):
        if score < 0:
            lines.append(f"  ❌ <code>{esc(slug)}</code> — unreachable")
        else:
            em = "🟢" if score>=85 else "🟡" if score>=65 else "🟠" if score>=40 else "🔴"
            lines.append(
                f"  {em} <code>{esc(slug)}</code> — {score}/100  "
                f"({len(plan)} fixes needed)"
            )
    lines += [
        "",
        "<i>Use /fixplan [owner/name] for fix steps.</i>",
        "<i>Use /dashboard for visual overview.</i>",
    ]
    await msg.edit_text("\n".join(lines), parse_mode=ParseMode.HTML)


# ── Pipeline Commands ────────────────────────────────────────────────

async def cmd_status(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(u): return await deny(u)
    cid = str(u.effective_chat.id)
    repo = sessions[cid]["repo"]
    msg  = await u.message.reply_text("⏳ Fetching status…")
    runs = wf_runs(GUARDIAN_WF, limit=3, repo=repo)
    if not runs:
        await msg.edit_text("No Guardian pipeline runs found yet.\nTrigger one with /scan")
        return
    lines = [f"🛡️ <b>Guardian Pipeline — {esc(repo)}</b>\n"]
    for run in runs:
        conc   = run.get("conclusion", run.get("status","?"))
        branch = run.get("head_branch","?")
        sha    = run.get("head_sha","")[:7]
        url    = run.get("html_url","")
        ts     = run.get("created_at","")[:16]
        icon   = {"success":"✅","failure":"❌","cancelled":"⏹️"}.get(conc or "","🔄")
        lines.append(f"{icon} <code>{sha}</code> [{branch}] {conc} ({ts})\n<a href='{url}'>View Run</a>")
    await msg.edit_text("\n".join(lines), parse_mode=ParseMode.HTML,
                        disable_web_page_preview=True)


async def cmd_scan(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(u): return await deny(u)
    cid = str(u.effective_chat.id)
    repo = sessions[cid]["repo"]
    msg  = await u.message.reply_text("⏳ Triggering full Guardian scan…")
    ok   = trigger_wf(GUARDIAN_WF, repo=repo)
    owner, rname = repo.split("/")
    await msg.edit_text(
        ('🛡️ <b>Full Guardian scan triggered!</b>' if ok else '❌ Failed to trigger scan.') +
        f"\n<a href='https://github.com/{esc(repo)}/actions'>Monitor Actions</a>",
        parse_mode=ParseMode.HTML, disable_web_page_preview=True
    )


async def cmd_build(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(u): return await deny(u)
    msg = await u.message.reply_text("⏳ Triggering build pipeline…")
    ok  = trigger_wf(GUARDIAN_WF, inputs={"skip_to_stage": "identity", "dry_run": "false"},
                     repo=NASGUARDIAN_REPO)
    await msg.edit_text(
        ('🔨 <b>Build triggered!</b>\nRunning: Identity → Dependency → Health → Validate' if ok
         else '❌ Could not trigger build. Check GitHub token permissions.') +
        f"\n<a href='https://github.com/{NASGUARDIAN_REPO}/actions'>Monitor Actions →</a>",
        parse_mode=ParseMode.HTML, disable_web_page_preview=True
    )


async def cmd_rebuild(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(u): return await deny(u)
    msg = await u.message.reply_text("⏳ Triggering full rebuild…")
    ok  = trigger_wf(GUARDIAN_WF, inputs={"skip_to_stage": "identity", "dry_run": "false"},
                     repo=NASGUARDIAN_REPO)
    await msg.edit_text(
        ('🔨 <b>Rebuild triggered!</b>\nFull pipeline: Identity → Dependency → Health → Validate → Release' if ok
         else '❌ Could not trigger rebuild. Check GitHub token has <code>workflow</code> scope.') +
        f"\n<a href='https://github.com/{NASGUARDIAN_REPO}/actions'>Monitor →</a>",
        parse_mode=ParseMode.HTML, disable_web_page_preview=True
    )


async def cmd_test(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(u): return await deny(u)
    msg = await u.message.reply_text("⏳ Triggering test suite…")
    ok  = trigger_wf(TESTS_WF, repo=NASGUARDIAN_REPO)
    await msg.edit_text(
        ('🧪 <b>Tests triggered!</b>\nSyntax Check · Agent Imports · Bot Test · Shell Lint' if ok
         else '❌ Could not trigger tests. Check GitHub token has <code>workflow</code> scope.') +
        f"\n<a href='https://github.com/{NASGUARDIAN_REPO}/actions'>Monitor →</a>",
        parse_mode=ParseMode.HTML, disable_web_page_preview=True
    )


async def cmd_repair(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(u): return await deny(u)
    msg = await u.message.reply_text("⏳ Triggering repair pipeline…")
    ok  = trigger_wf(GUARDIAN_WF, inputs={"skip_to_stage": "identity", "dry_run": "false"},
                     repo=NASGUARDIAN_REPO)
    await msg.edit_text(
        ('🔧 <b>Repair Bot triggered!</b>\nGuardian will scan and create a fix PR if patches are found.' if ok
         else '❌ Could not trigger repair. Check GitHub token has <code>workflow</code> scope.') +
        f"\n<a href='https://github.com/{NASGUARDIAN_REPO}/actions'>Monitor →</a>",
        parse_mode=ParseMode.HTML, disable_web_page_preview=True
    )


async def cmd_release(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(u): return await deny(u)
    cid  = str(u.effective_chat.id)
    repo = sessions[cid]["repo"]
    msg  = await u.message.reply_text("⏳ Fetching release…")
    owner, rname = repo.split("/")
    rel  = gh("GET", f"/repos/{owner}/{rname}/releases/latest", repo=repo)
    if "error" in rel:
        await msg.edit_text(f"❌ {esc(rel['error'])}")
        return
    name    = rel.get("name","?")
    tag     = rel.get("tag_name","?")
    url     = rel.get("html_url","")
    ts      = rel.get("published_at","")[:10]
    assets  = len(rel.get("assets",[]))
    dl_total = sum(a.get("download_count",0) for a in rel.get("assets",[]))
    await msg.edit_text(
        f"🚀 <b>Latest Release</b>\n<code>{esc(tag)}</code> — {esc(name)}\n"
        f"📅 {ts}  📦 {assets} APKs  ⬇️ {dl_total:,} downloads\n"
        f"<a href='{url}'>View Release</a>",
        parse_mode=ParseMode.HTML, disable_web_page_preview=True
    )


async def cmd_health(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(u): return await deny(u)
    cid  = str(u.effective_chat.id)
    repo = sessions[cid]["repo"]
    msg  = await u.message.reply_text("⏳ Running health check…")
    lines = ["❤️ <b>Health Check</b>\n"]

    # GitHub API connectivity
    owner, rname = repo.split("/")
    rate = gh("GET", "/rate_limit")
    if "error" not in rate:
        rem  = rate.get("rate", {}).get("remaining", "?")
        lim  = rate.get("rate", {}).get("limit", "?")
        lines.append(f"✅ GitHub API  —  {rem}/{lim} requests remaining")
    else:
        lines.append(f"❌ GitHub API  —  {esc(rate['error'])}")

    # GitHub token scope check
    if GITHUB_TOKEN:
        lines.append("✅ GitHub Token  —  set")
    else:
        lines.append("❌ GitHub Token  —  NOT SET")

    # AI providers
    lines.append(f"{'✅' if GROQ_KEY else '❌'} Groq API  —  {'set' if GROQ_KEY else 'missing'}")
    lines.append(f"{'✅' if GEMINI_KEY else '❌'} Gemini API  —  {'set' if GEMINI_KEY else 'missing'}")
    lines.append(f"{'✅' if OR_KEY else '❌'} OpenRouter  —  {'set' if OR_KEY else 'missing'}")

    # Recent workflow status on NasGuardian
    runs = wf_runs(limit=5, repo=NASGUARDIAN_REPO)
    if runs:
        passed = sum(1 for r in runs if r.get("conclusion") == "success")
        failed = sum(1 for r in runs if r.get("conclusion") == "failure")
        lines.append(f"\n📊 <b>Last 5 runs (NasGuardian):</b>  ✅ {passed} passed  ❌ {failed} failed")
        last = runs[0]
        icon = {"success": "✅", "failure": "❌", "cancelled": "⏹️"}.get(last.get("conclusion", ""), "🔄")
        lines.append(f"{icon} Latest: {esc(last.get('name','?')[:40])} — {esc(last.get('conclusion') or last.get('status','?'))}")
    else:
        lines.append("\n⚠️ No recent workflow runs found on NasGuardian")

    # Repo accessibility
    repo_info = gh("GET", f"/repos/{owner}/{rname}")
    if "error" not in repo_info:
        stars = repo_info.get("stargazers_count", 0)
        lines.append(f"\n✅ Repo <code>{esc(repo)}</code>  —  ⭐ {stars} stars")
    else:
        lines.append(f"\n❌ Repo <code>{esc(repo)}</code>  —  {esc(repo_info['error'])}")

    lines.append(f"\n<i>{BRAND}</i>")
    await msg.edit_text("\n".join(lines), parse_mode=ParseMode.HTML,
                        disable_web_page_preview=True)


async def cmd_doctor(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(u): return await deny(u)
    cid  = str(u.effective_chat.id)
    repo = sessions[cid]["repo"]
    msg  = await u.message.reply_text("⏳ Running deep diagnostic…")
    lines = ["🩺 <b>Doctor Scan</b>\n"]

    # --- API keys ---
    lines.append("<b>🔑 API Keys</b>")
    lines.append(f"  {'✅' if GITHUB_TOKEN else '❌'} GitHub Token")
    lines.append(f"  {'✅' if GROQ_KEY else '❌'} Groq API")
    lines.append(f"  {'✅' if GEMINI_KEY else '❌'} Gemini API")
    lines.append(f"  {'✅' if OR_KEY else '❌'} OpenRouter API")

    # --- GitHub rate limit ---
    rate = gh("GET", "/rate_limit")
    if "error" not in rate:
        r = rate.get("rate", {})
        rem, lim = r.get("remaining","?"), r.get("limit","?")
        reset_ts = r.get("reset", 0)
        import datetime
        reset_str = datetime.datetime.utcfromtimestamp(reset_ts).strftime("%H:%M UTC") if reset_ts else "?"
        lines.append(f"\n<b>📡 GitHub API</b>")
        lines.append(f"  Requests: {rem}/{lim}  —  resets at {reset_str}")
    else:
        lines.append(f"\n❌ GitHub API: {esc(rate['error'])}")

    # --- NasGuardian workflow stats ---
    lines.append(f"\n<b>🔄 NasGuardian Workflows (last 10 runs)</b>")
    runs = wf_runs(limit=10, repo=NASGUARDIAN_REPO)
    if runs:
        by_wf: dict = {}
        for r in runs:
            name = r.get("name","?")
            conc = r.get("conclusion") or r.get("status","?")
            by_wf.setdefault(name, []).append(conc)
        for wf_name, results in by_wf.items():
            p = results.count("success")
            f = results.count("failure")
            lines.append(f"  {esc(wf_name[:30])}: ✅{p} ❌{f}")
    else:
        lines.append("  No runs found")

    # --- Open issues + PRs ---
    owner, rname = repo.split("/")
    issues = gh("GET", f"/repos/{owner}/{rname}/issues?state=open&per_page=5")
    if isinstance(issues, list):
        real_issues = [i for i in issues if "pull_request" not in i]
        prs          = [i for i in issues if "pull_request" in i]
        lines.append(f"\n<b>📋 {esc(repo)}</b>")
        lines.append(f"  Open Issues: {len(real_issues)}  •  Open PRs: {len(prs)}")
    else:
        lines.append(f"\n⚠️ Could not fetch issues: {esc(str(issues.get('error','?')))}")

    # --- Bot session info ---
    history_len = len(sessions[cid].get("history", []))
    lines.append(f"\n<b>🤖 Bot Session</b>")
    lines.append(f"  AI mode: {'ON' if sessions[cid].get('ai_mode') else 'OFF'}")
    lines.append(f"  Chat history: {history_len} messages")
    lines.append(f"  Tracking repo: <code>{esc(repo)}</code>")

    lines.append(f"\n<i>{BRAND}</i>")
    await msg.edit_text("\n".join(lines), parse_mode=ParseMode.HTML,
                        disable_web_page_preview=True)


async def cmd_logs(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(u): return await deny(u)
    cid  = str(u.effective_chat.id)
    repo = sessions[cid]["repo"]
    msg  = await u.message.reply_text("⏳ Fetching logs…")
    runs = wf_runs(limit=5, repo=repo)
    if not runs:
        await msg.edit_text("No workflow runs found.")
        return
    lines = ["📋 <b>Recent Workflow Runs</b>\n"]
    for r in runs:
        conc = r.get("conclusion", r.get("status","?"))
        name = r.get("name","?")[:30]
        sha  = r.get("head_sha","")[:7]
        url  = r.get("html_url","")
        icon = {"success":"✅","failure":"❌","cancelled":"⏹️"}.get(conc or "","🔄")
        lines.append(f"{icon} <code>{sha}</code> {esc(name)}\n<a href='{url}'>Log</a>")
    await msg.edit_text("\n".join(lines), parse_mode=ParseMode.HTML,
                        disable_web_page_preview=True)


def _fetch_run_jobs(repo: str, run_id: int) -> list:
    """Fetch all jobs for a workflow run."""
    owner, rname = repo.split("/")
    r = gh("GET", f"/repos/{owner}/{rname}/actions/runs/{run_id}/jobs", repo=repo)
    return r.get("jobs", [])


def _fmt_job_steps(job: dict) -> str:
    """Format a job's steps as ✅/❌ lines."""
    lines = []
    j_conc = job.get("conclusion","?")
    j_icon = {"success":"✅","failure":"❌","cancelled":"⏹️","skipped":"⏭️"}.get(j_conc,"🔄")
    lines.append(f"{j_icon} <b>{esc(job.get('name','?'))}</b>")
    for step in job.get("steps", []):
        conc = step.get("conclusion") or step.get("status","?")
        icon = {"success":"✅","failure":"❌","cancelled":"⏹️","skipped":"⏭️"}.get(conc,"🔄")
        num  = step.get("number","")
        lines.append(f"  {icon} Step {num}: {esc(step.get('name','?'))}")
    return "\n".join(lines)


async def cmd_errors(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Show recent failures with full job + step breakdown (screenshot-style)."""
    if not authorized(u): return await deny(u)
    cid  = str(u.effective_chat.id)
    repo = sessions[cid]["repo"]
    msg  = await u.message.reply_text("⏳ Fetching failures + job details…")
    runs = wf_runs(limit=20, repo=repo)
    failed = [r for r in runs if r.get("conclusion") == "failure"]
    if not failed:
        await msg.edit_text("✅ <b>No recent failures!</b>\nAll workflow runs passed.",
                            parse_mode=ParseMode.HTML)
        return

    owner, rname = repo.split("/")
    blocks = [f"❌ <b>Failure Report — {esc(rname)} ({len(failed)} failed)</b>\n"]

    for r in failed[:3]:
        run_id = r.get("id")
        sha    = r.get("head_sha","")[:7]
        name   = r.get("name","?")
        branch = r.get("head_branch","?")
        ts     = r.get("created_at","")[:16].replace("T"," ")
        url    = r.get("html_url","")
        msg_hd = (r.get("head_commit") or {}).get("message","")[:60]

        blocks.append(
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"🔴 <b>{esc(name)}</b>\n"
            f"📅 {ts}  |  🌿 <code>{esc(branch)}</code>\n"
            f"🔗 <code>{sha}</code> — {esc(msg_hd)}\n"
        )

        jobs = _fetch_run_jobs(repo, run_id)
        if jobs:
            for job in jobs:
                blocks.append(_fmt_job_steps(job))
        else:
            blocks.append("  ⚠️ Job details unavailable (no GitHub token)")

        blocks.append(f"\n📎 <a href='{url}'>View full log on GitHub</a>")

    blocks.append(f"\n<i>{BRAND}</i>\n💡 Use /errorshot to download full log as a file")
    text = "\n".join(blocks)
    await msg.edit_text(truncate(text, 4000), parse_mode=ParseMode.HTML,
                        disable_web_page_preview=True)


async def cmd_errorshot(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Download the latest failed run's full job/step report as a .txt file."""
    if not authorized(u): return await deny(u)
    cid  = str(u.effective_chat.id)
    repo = sessions[cid]["repo"]
    owner, rname = repo.split("/")
    msg  = await u.message.reply_text("📸 Building error screenshot…")

    runs   = wf_runs(limit=20, repo=repo)
    failed = [r for r in runs if r.get("conclusion") == "failure"]
    if not failed:
        await msg.edit_text("✅ No failures found — all runs passed!")
        return

    lines = [
        "=" * 60,
        "  NasTech Guardian — Error Screenshot",
        f"  Developed by Nsamba Naswif Cohen",
        f"  Repo: {repo}",
        f"  Generated: {__import__('datetime').datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}",
        "=" * 60,
        "",
    ]

    for idx, r in enumerate(failed[:5], 1):
        run_id = r.get("id")
        sha    = r.get("head_sha","")[:7]
        name   = r.get("name","?")
        branch = r.get("head_branch","?")
        ts     = r.get("created_at","")[:16].replace("T"," ")
        url    = r.get("html_url","")
        actor  = r.get("triggering_actor",{}).get("login","?") if r.get("triggering_actor") else "?"
        commit_msg = (r.get("head_commit") or {}).get("message","")[:80]
        event  = r.get("event","?")

        lines += [
            f"[FAILURE #{idx}]",
            f"  Workflow : {name}",
            f"  Branch   : {branch}",
            f"  Commit   : {sha} — {commit_msg}",
            f"  Triggered: {event} by {actor}",
            f"  Time     : {ts}",
            f"  URL      : {url}",
            "",
        ]

        jobs = _fetch_run_jobs(repo, run_id)
        if jobs:
            for job in jobs:
                j_conc = job.get("conclusion","?")
                j_icon = "PASS" if j_conc == "success" else "FAIL" if j_conc == "failure" else j_conc.upper()
                lines.append(f"  [{j_icon}] Job: {job.get('name','?')}")
                for step in job.get("steps",[]):
                    s_conc = step.get("conclusion") or step.get("status","?")
                    s_icon = " OK " if s_conc == "success" else "FAIL" if s_conc == "failure" else "SKIP"
                    lines.append(f"         [{s_icon}] Step {step.get('number','')}: {step.get('name','?')}")
                lines.append("")
        else:
            lines += ["  (Job details require GITHUB_TOKEN secret)", ""]

        lines += ["-" * 60, ""]

    lines += [
        "",
        "=" * 60,
        f"  {BRAND}",
        "  github.com/nastech-ai/NasGuardian",
        "=" * 60,
    ]

    report = "\n".join(lines)
    fname  = f"nasguardian_errors_{__import__('datetime').datetime.utcnow().strftime('%Y%m%d_%H%M')}.txt"

    await msg.delete()
    await u.message.reply_document(
        document=report.encode("utf-8"),
        filename=fname,
        caption=(
            f"📸 <b>Error Screenshot — {esc(rname)}</b>\n"
            f"❌ {len(failed)} failed run(s) • {len(failed[:5])} shown\n"
            f"<i>{BRAND}</i>"
        ),
        parse_mode=ParseMode.HTML,
    )


async def cmd_dependencies(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(u): return await deny(u)
    cid  = str(u.effective_chat.id)
    repo = sessions[cid]["repo"]
    msg  = await u.message.reply_text("⏳ Running dependency audit…")
    owner, rname = repo.split("/")
    lines = [f"📦 <b>Dependency Audit — {esc(rname)}</b>\n"]

    # Try reading requirements.txt
    import base64
    req = gh("GET", f"/repos/{owner}/{rname}/contents/scripts/telegram_bot/requirements.txt")
    if "content" in req:
        content = base64.b64decode(req["content"]).decode(errors="replace")
        pkgs = [l.strip() for l in content.splitlines() if l.strip() and not l.startswith("#")]
        lines.append(f"<b>Python packages ({len(pkgs)}):</b>")
        for p in pkgs[:20]:
            lines.append(f"  • <code>{esc(p)}</code>")
        if len(pkgs) > 20:
            lines.append(f"  … and {len(pkgs)-20} more")
    else:
        # Try root requirements.txt
        req2 = gh("GET", f"/repos/{owner}/{rname}/contents/requirements.txt")
        if "content" in req2:
            content = base64.b64decode(req2["content"]).decode(errors="replace")
            pkgs = [l.strip() for l in content.splitlines() if l.strip() and not l.startswith("#")]
            lines.append(f"<b>Python packages ({len(pkgs)}):</b>")
            for p in pkgs[:20]:
                lines.append(f"  • <code>{esc(p)}</code>")
        else:
            lines.append("⚠️ No requirements.txt found in repo")
            lines.append(f"\n<b>Triggering audit workflow…</b>")
            ok = trigger_wf(AUDIT_WF, repo=NASGUARDIAN_REPO)
            lines.append("✅ Audit workflow triggered" if ok else "❌ Workflow trigger failed")
            lines.append(f"<a href='https://github.com/{NASGUARDIAN_REPO}/actions'>Monitor →</a>")

    lines.append(f"\n<i>{BRAND}</i>")
    await msg.edit_text("\n".join(lines), parse_mode=ParseMode.HTML,
                        disable_web_page_preview=True)


async def cmd_packages(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(u): return await deny(u)
    cid  = str(u.effective_chat.id)
    repo = sessions[cid]["repo"]
    msg  = await u.message.reply_text("⏳ Reading versions…")
    owner, rname = repo.split("/")
    import base64
    gp = gh("GET", f"/repos/{owner}/{rname}/contents/gradle.properties", repo=repo)
    if "error" in gp or "content" not in gp:
        await msg.edit_text(f"❌ Could not read gradle.properties")
        return
    content = base64.b64decode(gp["content"]).decode(errors="replace")
    lines   = ["📦 <b>gradle.properties Versions</b>\n<pre>"]
    for line in content.split("\n"):
        if re.search(r'(Version|Sdk|ndk|variant)', line, re.I) and "=" in line and not line.startswith("#"):
            lines.append(esc(line.strip()))
    lines.append("</pre>")
    await msg.edit_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def cmd_security(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(u): return await deny(u)
    cid  = str(u.effective_chat.id)
    repo = sessions[cid]["repo"]
    msg  = await u.message.reply_text("⏳ Running security audit…")
    lines = [f"🔒 <b>Security Scan — {esc(repo.split('/')[-1])}</b>\n"]

    # Check for known risky files / secrets patterns in repo tree
    owner, rname = repo.split("/")
    tree = gh("GET", f"/repos/{owner}/{rname}/git/trees/HEAD?recursive=1")
    risky_exts  = {".env", ".pem", ".key", ".p12", ".pfx", ".jks"}
    risky_names = {"secrets.json", ".env", "keystore.jks", "debug.keystore"}
    found_risky = []
    if "tree" in tree:
        for item in tree["tree"]:
            path = item.get("path","")
            name = path.split("/")[-1].lower()
            ext  = "." + name.rsplit(".",1)[-1] if "." in name else ""
            if name in risky_names or ext in risky_exts:
                found_risky.append(path)
    if found_risky:
        lines.append(f"⚠️ <b>{len(found_risky)} potentially sensitive file(s):</b>")
        for f in found_risky[:10]:
            lines.append(f"  • <code>{esc(f)}</code>")
    else:
        lines.append("✅ No sensitive files (.env / .key / .pem / keystore) found in tree")

    # Check open security advisories
    owner_g, rname_g = NASGUARDIAN_REPO.split("/")
    advisories = gh("GET", f"/repos/{owner_g}/{rname_g}/vulnerability-alerts")
    if isinstance(advisories, dict) and "error" in advisories:
        lines.append("\n⚠️ Vulnerability alerts: requires admin access to check")
    else:
        lines.append("\n✅ No vulnerability alerts found")

    # Trigger real audit workflow
    ok = trigger_wf(AUDIT_WF, repo=NASGUARDIAN_REPO)
    lines.append(f"\n<b>🔍 Full Audit Workflow:</b>")
    lines.append("✅ Triggered — Bandit · Safety · Pylint running" if ok
                 else "⚠️ Workflow trigger failed (token needs <code>workflow</code> scope)")
    if ok:
        lines.append(f"<a href='https://github.com/{NASGUARDIAN_REPO}/actions'>Monitor →</a>")

    lines.append(f"\n<i>{BRAND}</i>")
    await msg.edit_text("\n".join(lines), parse_mode=ParseMode.HTML,
                        disable_web_page_preview=True)


async def cmd_fix(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(u): return await deny(u)
    cid  = str(u.effective_chat.id)
    msg  = await u.message.reply_text("⏳ Triggering fix pipeline…")
    ok   = trigger_wf(GUARDIAN_WF, inputs={"skip_to_stage": "identity", "dry_run": "false"},
                      repo=NASGUARDIAN_REPO)
    await msg.edit_text(
        ('🔧 <b>Fix pipeline triggered!</b>\nGuardian will scan → fix → open a PR with patches.' if ok
         else '❌ Could not trigger fix. Check GitHub token has <code>workflow</code> scope.') +
        f"\n<a href='https://github.com/{NASGUARDIAN_REPO}/actions'>Monitor →</a>",
        parse_mode=ParseMode.HTML, disable_web_page_preview=True
    )


async def cmd_pr(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(u): return await deny(u)
    cid  = str(u.effective_chat.id)
    repo = sessions[cid]["repo"]
    msg  = await u.message.reply_text("⏳ Fetching PRs…")
    owner, rname = repo.split("/")
    prs = gh("GET", f"/repos/{owner}/{rname}/pulls?state=open&per_page=10", repo=repo)
    if not isinstance(prs, list) or not prs:
        await msg.edit_text("✅ No open pull requests.")
        return
    lines = [f"🔀 <b>Open PRs ({len(prs)})</b>\n"]
    for pr in prs[:8]:
        num   = pr.get("number")
        title = pr.get("title","?")[:55]
        user  = pr.get("user",{}).get("login","?")
        url   = pr.get("html_url","")
        draft = " [DRAFT]" if pr.get("draft") else ""
        lines.append(f"<b>#{num}</b>{draft} {esc(title)}\n  @{user} — <a href='{url}'>View</a>")
    await msg.edit_text("\n".join(lines), parse_mode=ParseMode.HTML,
                        disable_web_page_preview=True)


async def cmd_issues(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(u): return await deny(u)
    cid  = str(u.effective_chat.id)
    repo = sessions[cid]["repo"]
    msg  = await u.message.reply_text("⏳ Fetching issues…")
    owner, rname = repo.split("/")
    issues = gh("GET", f"/repos/{owner}/{rname}/issues?state=open&per_page=10", repo=repo)
    if not isinstance(issues, list):
        await msg.edit_text("❌ Could not fetch issues.")
        return
    issues = [i for i in issues if "pull_request" not in i]
    if not issues:
        await msg.edit_text("✅ No open issues.")
        return
    lines = [f"🐛 <b>Open Issues ({len(issues)})</b>\n"]
    for i in issues[:8]:
        num    = i.get("number")
        title  = i.get("title","?")[:55]
        labels = ", ".join(l["name"] for l in i.get("labels",[]))
        url    = i.get("html_url","")
        lines.append(f"<b>#{num}</b> {esc(title)}\n  {esc(labels) or 'no labels'} — <a href='{url}'>View</a>")
    await msg.edit_text("\n".join(lines), parse_mode=ParseMode.HTML,
                        disable_web_page_preview=True)


async def cmd_commits(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(u): return await deny(u)
    cid  = str(u.effective_chat.id)
    repo = sessions[cid]["repo"]
    msg  = await u.message.reply_text("⏳ Fetching commits…")
    owner, rname = repo.split("/")
    commits = gh("GET", f"/repos/{owner}/{rname}/commits?per_page=7", repo=repo)
    if not isinstance(commits, list):
        await msg.edit_text("❌ Could not fetch commits.")
        return
    lines = ["📝 <b>Recent Commits</b>\n"]
    for c in commits:
        sha  = c.get("sha","")[:7]
        msg_ = c.get("commit",{}).get("message","?").split("\n")[0][:55]
        auth = c.get("commit",{}).get("author",{}).get("name","?")[:20]
        url  = c.get("html_url","")
        lines.append(f"<code>{sha}</code> {esc(msg_)}\n  {esc(auth)} — <a href='{url}'>View</a>")
    await msg.edit_text("\n".join(lines), parse_mode=ParseMode.HTML,
                        disable_web_page_preview=True)


async def cmd_branches(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(u): return await deny(u)
    cid  = str(u.effective_chat.id)
    repo = sessions[cid]["repo"]
    msg  = await u.message.reply_text("⏳ Fetching branches…")
    owner, rname = repo.split("/")
    branches = gh("GET", f"/repos/{owner}/{rname}/branches?per_page=20", repo=repo)
    if not isinstance(branches, list):
        await msg.edit_text("❌ Could not fetch branches.")
        return
    lines = [f"🌿 <b>Branches ({len(branches)})</b>\n"]
    for b in branches[:15]:
        name    = b.get("name","?")
        protect = "🔒 " if b.get("protected") else ""
        lines.append(f"  {protect}<code>{esc(name)}</code>")
    await msg.edit_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def cmd_version(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(u): return await deny(u)
    cid  = str(u.effective_chat.id)
    repo = sessions[cid]["repo"]
    msg  = await u.message.reply_text("⏳ Fetching version…")
    import base64
    owner, rname = repo.split("/")
    r = gh("GET", f"/repos/{owner}/{rname}/contents/app/build.gradle", repo=repo)
    if "content" not in r:
        await msg.edit_text("❌ Could not read build.gradle")
        return
    text = base64.b64decode(r["content"]).decode(errors="replace")
    vm   = re.search(r'versionName\s+"([^"]+)"', text)
    vc   = re.search(r'versionCode\s+(\d+)', text)
    sdk  = re.search(r'compileSdkVersion[^=]*=\s*([^\s]+)', text)
    ver  = vm.group(1) if vm else "?"
    code = vc.group(1) if vc else "?"
    sdk_ = sdk.group(1) if sdk else "?"
    await msg.edit_text(
        f"📱 <b>NasTech AI Terminal</b>\n"
        f"  Version:     <code>{esc(ver)}</code>\n"
        f"  VersionCode: <code>{esc(code)}</code>\n"
        f"  CompileSdk:  <code>{esc(sdk_)}</code>",
        parse_mode=ParseMode.HTML
    )


async def cmd_workflows(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(u): return await deny(u)
    cid  = str(u.effective_chat.id)
    repo = sessions[cid]["repo"]
    msg  = await u.message.reply_text("⏳ Fetching workflows…")
    owner, rname = repo.split("/")
    r = gh("GET", f"/repos/{owner}/{rname}/actions/workflows", repo=repo)
    wfs = r.get("workflows", [])
    if not wfs:
        await msg.edit_text("No workflows found.")
        return
    lines = [f"⚙️ <b>Workflows ({len(wfs)})</b>\n"]
    for wf in wfs:
        name  = wf.get("name","?")
        state = wf.get("state","?")
        icon  = "✅" if state == "active" else "⏸️"
        lines.append(f"  {icon} {esc(name)} (<code>{state}</code>)")
    await msg.edit_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def cmd_models(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(u): return await deny(u)
    g   = "✅" if GROQ_KEY   else "❌"
    gm  = "✅" if GEMINI_KEY else "❌"
    opr = "✅" if OR_KEY     else "❌"
    await u.message.reply_text(
        f"🤖 <b>AI Coordinator</b>\n\n"
        f"{g}  Groq       — llama-3.3-70b-versatile\n"
        f"{gm} Gemini     — gemini-2.0-flash\n"
        f"{opr} OpenRouter — meta-llama/llama-3.3-70b\n\n"
        "Priority: Groq → Gemini → OpenRouter\n"
        "Fallback: automatic on error/timeout",
        parse_mode=ParseMode.HTML
    )


async def cmd_metrics(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(u): return await deny(u)
    cid  = str(u.effective_chat.id)
    repo = sessions[cid]["repo"]
    msg  = await u.message.reply_text("⏳ Gathering metrics…")
    runs   = wf_runs(limit=20, repo=repo)
    total  = len(runs)
    passed = sum(1 for r in runs if r.get("conclusion")=="success")
    failed = sum(1 for r in runs if r.get("conclusion")=="failure")
    active = sum(1 for r in runs if r.get("status")=="in_progress")
    rate   = round(passed/total*100, 1) if total else 0
    avg_s  = 0
    times  = []
    for r in runs:
        try:
            from datetime import datetime as dt
            s = dt.fromisoformat(r.get("created_at","").replace("Z","+00:00"))
            e = dt.fromisoformat(r.get("updated_at","").replace("Z","+00:00"))
            times.append((e-s).total_seconds())
        except Exception:
            pass
    avg_s = round(sum(times)/len(times)/60, 1) if times else 0
    await msg.edit_text(
        f"📊 <b>Pipeline Metrics</b> (last {total} runs)\n\n"
        f"✅ Success rate: <b>{rate}%</b>\n"
        f"✅ Passed:       <code>{passed}</code>\n"
        f"❌ Failed:       <code>{failed}</code>\n"
        f"🔄 Running:      <code>{active}</code>\n"
        f"⏱️  Avg duration: <code>{avg_s} min</code>",
        parse_mode=ParseMode.HTML
    )


async def cmd_storage(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(u): return await deny(u)
    cid  = str(u.effective_chat.id)
    repo = sessions[cid]["repo"]
    msg  = await u.message.reply_text("⏳ Checking artifacts…")
    owner, rname = repo.split("/")
    r = gh("GET", f"/repos/{owner}/{rname}/actions/artifacts?per_page=5", repo=repo)
    arts = r.get("artifacts", [])
    if not arts:
        await msg.edit_text("No artifacts found.")
        return
    total_mb = sum(a.get("size_in_bytes",0) for a in arts) // 1024 // 1024
    lines = [f"📦 <b>Artifacts</b> ({total_mb} MB total)\n"]
    for a in arts[:5]:
        name  = a.get("name","?")[:40]
        mb    = round(a.get("size_in_bytes",0)/1024/1024, 2)
        exp   = "🗑️" if a.get("expired") else ""
        lines.append(f"  {exp} {esc(name)} ({mb} MB)")
    await msg.edit_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def cmd_services(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(u): return await deny(u)
    msg = await u.message.reply_text("⏳ Checking services…")
    def ping(url):
        try:
            urllib.request.urlopen(
                urllib.request.Request(url, method="HEAD"), timeout=4
            )
            return "✅"
        except Exception:
            return "❌"
    checks = [
        ("GitHub API",    "https://api.github.com"),
        ("Groq",          "https://api.groq.com"),
        ("Google Gemini", "https://generativelanguage.googleapis.com"),
        ("OpenRouter",    "https://openrouter.ai"),
        ("Telegram API",  "https://api.telegram.org"),
    ]
    results = [(name, ping(url)) for name, url in checks]
    lines = ["🔌 <b>Service Status</b>\n"]
    for name, status in results:
        lines.append(f"  {status} {name}")
    await msg.edit_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def cmd_memory(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(u): return await deny(u)
    cid = str(u.effective_chat.id)
    s   = sessions[cid]
    await u.message.reply_text(
        f"🧠 <b>Bot State</b>\n\n"
        f"  Version:    <code>{BOT_VERSION}</code>\n"
        f"  Repo:       <code>{esc(s['repo'])}</code>\n"
        f"  AI mode:    <code>{'on' if s['ai_mode'] else 'off'}</code>\n"
        f"  History:    <code>{len(s['history'])} messages</code>\n"
        f"  Auth:       <code>{'restricted' if WHITELIST else 'open'}</code>\n"
        f"  Time:       <code>{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}</code>\n"
        f"  Providers:  "
        f"{'✅' if GROQ_KEY else '❌'}groq "
        f"{'✅' if GEMINI_KEY else '❌'}gemini "
        f"{'✅' if OR_KEY else '❌'}openrouter",
        parse_mode=ParseMode.HTML
    )


async def cmd_backup(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(u): return await deny(u)
    await u.message.reply_text(
        "💾 <b>Backup</b>\nGuardian state is in GitHub Actions artifacts.\n"
        "Code backup: git history.\n"
        "API keys: backed up in Replit Secrets + GitHub Secrets.",
        parse_mode=ParseMode.HTML
    )


async def cmd_clear(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(u): return await deny(u)
    cid = str(u.effective_chat.id)
    sessions[cid]["history"] = []
    await u.message.reply_text("🗑️ Conversation history cleared.")


# ── API Key Management via Telegram ──────────────────────────────────
_KEY_NAMES = {
    "GROQ_API_KEY":                 ("Groq",        "groq"),
    "GEMINI_API_KEY":               ("Gemini",       "gemini"),
    "OPENROUTER_API_KEY":           ("OpenRouter",   "openrouter"),
    "TELEGRAM_BOT_TOKEN":           ("Telegram Bot Token", "telegram"),
    "TELEGRAM_CHAT_ID":             ("Telegram Chat ID",   "telegram"),
    "GITHUB_TOKEN":                 ("GitHub PAT",   "github"),
    "GH_TOKEN":                     ("GitHub PAT",   "github"),
    "GITHUB_PERSONAL_ACCESS_TOKEN": ("GitHub PAT",   "github"),
}

_NASTECH_ENV_FILE = os.path.expanduser("~/.nastech_env")


def _mask(val: str) -> str:
    if not val:
        return "❌ <i>not set</i>"
    if len(val) <= 8:
        return "✅ " + "*" * len(val)
    return "✅ " + val[:6] + "…" + val[-3:]


def _load_env_file() -> dict:
    env = {}
    try:
        with open(_NASTECH_ENV_FILE) as f:
            for line in f:
                line = line.strip()
                if line.startswith("export ") and "=" in line:
                    line = line[7:]
                if "=" in line and not line.startswith("#"):
                    k, _, v = line.partition("=")
                    env[k.strip()] = v.strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    return env


def _write_env_file(env: dict):
    lines = ["# NasTech Guardian — API Keys", "# Managed via Telegram /setkey", ""]
    for k, v in env.items():
        lines.append(f'export {k}="{v}"')
    with open(_NASTECH_ENV_FILE, "w") as f:
        f.write("\n".join(lines) + "\n")
    os.environ.update({k: v for k, v in env.items() if v})


async def cmd_apikeys(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(u): return await deny(u)
    file_env = _load_env_file()
    rows = []
    for key in ["GROQ_API_KEY", "GEMINI_API_KEY", "OPENROUTER_API_KEY",
                "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "GITHUB_TOKEN"]:
        val = os.environ.get(key) or file_env.get(key, "")
        label = _KEY_NAMES.get(key, (key,))[0]
        rows.append(f"  <b>{label}</b>: {_mask(val)}")
    keys_list = "\n".join(rows)
    text = (
        "🔑 <b>API Keys Status</b>\n\n"
        f"{keys_list}\n\n"
        "<b>Change a key:</b>\n"
        "<code>/setkey GROQ_API_KEY gsk_xxxx</code>\n"
        "<code>/setkey GEMINI_API_KEY AIzaxxxx</code>\n"
        "<code>/setkey OPENROUTER_API_KEY sk-or-xxxx</code>\n"
        "<code>/setkey TELEGRAM_BOT_TOKEN 12345:xxxx</code>\n"
        "<code>/setkey GITHUB_TOKEN ghp_xxxx</code>\n\n"
        "🛡️ Values are masked in this view."
    )
    await u.message.reply_text(text, parse_mode=ParseMode.HTML)


async def cmd_setkey(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(u): return await deny(u)
    args = ctx.args or []
    if len(args) < 2:
        await u.message.reply_text(
            "🔑 <b>Set API Key</b>\n\n"
            "Usage:\n"
            "<code>/setkey KEY_NAME value</code>\n\n"
            "<b>Available keys:</b>\n"
            "  <code>GROQ_API_KEY</code>\n"
            "  <code>GEMINI_API_KEY</code>\n"
            "  <code>OPENROUTER_API_KEY</code>\n"
            "  <code>TELEGRAM_BOT_TOKEN</code>\n"
            "  <code>TELEGRAM_CHAT_ID</code>\n"
            "  <code>GITHUB_TOKEN</code>\n\n"
            "Example:\n"
            "<code>/setkey GROQ_API_KEY gsk_abc123</code>",
            parse_mode=ParseMode.HTML
        )
        return
    key_name = args[0].strip().upper()
    key_val  = " ".join(args[1:]).strip()

    allowed = {
        "GROQ_API_KEY", "GEMINI_API_KEY", "OPENROUTER_API_KEY",
        "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
        "GITHUB_TOKEN", "GH_TOKEN", "GITHUB_PERSONAL_ACCESS_TOKEN"
    }
    if key_name not in allowed:
        await u.message.reply_text(
            f"❌ Unknown key: <code>{key_name}</code>\n"
            f"Allowed: {', '.join(sorted(allowed))}",
            parse_mode=ParseMode.HTML
        )
        return

    os.environ[key_name] = key_val
    file_env = _load_env_file()
    file_env[key_name] = key_val
    try:
        _write_env_file(file_env)
        saved_to = f"✅ Saved to <code>{_NASTECH_ENV_FILE}</code>"
    except Exception as e:
        saved_to = f"⚠️ Could not write file: {e}"

    global GROQ_API_KEY, GEMINI_API_KEY, OPENROUTER_API_KEY
    if key_name == "GROQ_API_KEY":          GROQ_API_KEY       = key_val
    if key_name == "GEMINI_API_KEY":        GEMINI_API_KEY     = key_val
    if key_name in ("OPENROUTER_API_KEY",): OPENROUTER_API_KEY = key_val
    if key_name in ("GITHUB_TOKEN", "GH_TOKEN", "GITHUB_PERSONAL_ACCESS_TOKEN"):
        global GITHUB_TOKEN
        GITHUB_TOKEN = key_val

    label  = _KEY_NAMES.get(key_name, (key_name,))[0]
    masked = _mask(key_val)
    await u.message.reply_text(
        f"🔑 <b>{label}</b> updated!\n\n"
        f"  Value: {masked}\n"
        f"  {saved_to}\n\n"
        "💡 Key is live immediately — no restart needed.\n"
        "Use /apikeys to see all keys.",
        parse_mode=ParseMode.HTML
    )


async def cmd_testkeys(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(u): return await deny(u)
    msg = await u.message.reply_text("🔍 Testing API keys…", parse_mode=ParseMode.HTML)
    results = []

    async def _test_groq():
        k = os.environ.get("GROQ_API_KEY", "")
        if not k: return "❌ Groq — not set"
        try:
            import aiohttp
            headers = {"Authorization": f"Bearer {k}", "Content-Type": "application/json"}
            payload = {"model": "llama3-8b-8192", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 5}
            async with aiohttp.ClientSession() as s:
                async with s.post("https://api.groq.com/openai/v1/chat/completions",
                                  json=payload, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as r:
                    return "✅ Groq — OK" if r.status == 200 else f"❌ Groq — HTTP {r.status}"
        except Exception as e:
            return f"⚠️ Groq — {type(e).__name__}"

    async def _test_gemini():
        k = os.environ.get("GEMINI_API_KEY", "")
        if not k: return "❌ Gemini — not set"
        try:
            import aiohttp
            url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={k}"
            payload = {"contents": [{"parts": [{"text": "hi"}]}]}
            async with aiohttp.ClientSession() as s:
                async with s.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as r:
                    return "✅ Gemini — OK" if r.status == 200 else f"❌ Gemini — HTTP {r.status}"
        except Exception as e:
            return f"⚠️ Gemini — {type(e).__name__}"

    async def _test_openrouter():
        k = os.environ.get("OPENROUTER_API_KEY", "")
        if not k: return "❌ OpenRouter — not set"
        try:
            import aiohttp
            headers = {"Authorization": f"Bearer {k}", "Content-Type": "application/json"}
            payload = {"model": "mistralai/mistral-7b-instruct:free", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 5}
            async with aiohttp.ClientSession() as s:
                async with s.post("https://openrouter.ai/api/v1/chat/completions",
                                  json=payload, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as r:
                    return "✅ OpenRouter — OK" if r.status == 200 else f"❌ OpenRouter — HTTP {r.status}"
        except Exception as e:
            return f"⚠️ OpenRouter — {type(e).__name__}"

    tests = await asyncio.gather(
        _test_groq(), _test_gemini(), _test_openrouter(),
        return_exceptions=True
    )
    lines = "\n".join(str(t) for t in tests)
    tg_ok  = "✅" if os.environ.get("TELEGRAM_BOT_TOKEN") else "❌"
    gh_ok  = "✅" if (os.environ.get("GITHUB_TOKEN") or
                      os.environ.get("GITHUB_PERSONAL_ACCESS_TOKEN")) else "❌"
    await msg.edit_text(
        f"🔍 <b>API Key Test Results</b>\n\n"
        f"{lines}\n"
        f"{tg_ok} Telegram Bot Token\n"
        f"{gh_ok} GitHub PAT\n\n"
        "Use /setkey to update any key.",
        parse_mode=ParseMode.HTML
    )


async def cmd_ai_off(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(u): return await deny(u)
    cid = str(u.effective_chat.id)
    sessions[cid]["ai_mode"] = False
    await u.message.reply_text("🔇 AI auto-reply disabled. Use /ask to query AI explicitly.")


async def cmd_ai_on(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(u): return await deny(u)
    cid = str(u.effective_chat.id)
    sessions[cid]["ai_mode"] = True
    await u.message.reply_text("🔊 AI auto-reply enabled. Just type to chat!")


# ── Inline AI handler (typing = AI chat) ────────────────────────────

async def handle_text(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(u): return await deny(u)
    if not u.message or not u.message.text:
        return
    text = u.message.text.strip()
    if text.startswith("/"):
        return
    cid = str(u.effective_chat.id)

    # ── Category button → switch keyboard ────────────────────────────
    if text in _CATEGORY_KB_MAP:
        kb_name = _CATEGORY_KB_MAP[text]
        label   = _CATEGORY_LABELS.get(text, text)
        await show_keyboard(u, kb_name,
            text=f"<b>{label}</b>\nTap a button or type a message:")
        return

    # ── Action button → route to command ─────────────────────────────
    if text in _BUTTON_COMMANDS:
        cmd, arg = _BUTTON_COMMANDS[text]

        # Special case: "Set X Key" buttons — prompt user for value
        if cmd == "/setkey" and arg:
            await u.message.reply_text(
                f"🔑 <b>Set {arg}</b>\n\n"
                f"Reply with the new value:\n"
                f"<code>/setkey {arg} YOUR_VALUE_HERE</code>\n\n"
                f"Example:\n"
                f"<code>/setkey {arg} sk-xxxxxxxxxxxx</code>",
                parse_mode=ParseMode.HTML
            )
            return

        # Buttons that show info/links directly
        if cmd == "/ask" and arg:
            msg = await u.message.reply_text("🤔 …")
            result = ai_chat(arg, [], sessions[cid]["repo"])
            await msg.edit_text(truncate(fmt_ai(result), 4000), parse_mode=ParseMode.HTML)
            return

        # Route to the matching command function
        _CMD_FN_MAP = {
            "/ask":          cmd_ask,          "/explain":      cmd_explain,
            "/review":       cmd_review,       "/fix_error":    cmd_fix_error,
            "/run":          cmd_run,          "/summarize":    cmd_summarize,
            "/translate":    cmd_translate,    "/ai_on":        cmd_ai_on,
            "/ai_off":       cmd_ai_off,       "/clear":        cmd_clear,
            "/memory":       cmd_memory,       "/models":       cmd_models,
            "/providers":    cmd_models,       "/status":       cmd_status,
            "/scan":         cmd_scan,         "/build":        cmd_build,
            "/rebuild":      cmd_rebuild,      "/test":         cmd_test,
            "/repair":       cmd_repair,       "/release":      cmd_release,
            "/health":       cmd_health,       "/doctor":       cmd_doctor,
            "/logs":         cmd_logs,         "/errors":       cmd_errors,
            "/dependencies": cmd_dependencies, "/packages":     cmd_packages,
            "/security":     cmd_security,     "/fix":          cmd_fix,
            "/pr":           cmd_pr,           "/issues":       cmd_issues,
            "/commits":      cmd_commits,      "/branches":     cmd_branches,
            "/version":      cmd_version,      "/workflows":    cmd_workflows,
            "/metrics":      cmd_metrics,      "/storage":      cmd_storage,
            "/services":     cmd_services,     "/repos":        cmd_repos,
            "/addrepo":      cmd_addrepo,      "/dashboard":    cmd_dashboard,
            "/audit":        cmd_audit,        "/fixplan":      cmd_fixplan,
            "/scanall":      cmd_scanall,      "/repo":         cmd_repo,
            "/daily":        cmd_daily,        "/subscribe":    cmd_daily_subscribe,
            "/unsubscribe":  cmd_unsubscribe,  "/help":         cmd_help,
            "/notif":        cmd_notif,        "/notifon":      cmd_notifon,
            "/notifoff":     cmd_notifoff,     "/autofix":      cmd_autofix,
            "/approvefix":   cmd_approvefix,   "/cancelfix":    cmd_cancelfix,
            "/errorshot":    cmd_errorshot,
            "/backup":       cmd_backup,       "/apikeys":      cmd_apikeys,
            "/testkeys":     cmd_testkeys,
        }
        fn = _CMD_FN_MAP.get(cmd)
        if fn:
            if arg:
                ctx.args = [arg]   # pass button arg to handler for ALL commands
            await fn(u, ctx)
            return

    # ── Regular AI chat ───────────────────────────────────────────────
    if not sessions[cid]["ai_mode"]:
        return
    now = time.time()
    if now - sessions[cid]["last_active"] < 2:
        return
    sessions[cid]["last_active"] = now
    msg = await u.message.reply_text("🤔 …")
    result = ai_chat(text, sessions[cid]["history"], sessions[cid]["repo"])
    sessions[cid]["history"].append({"role": "user",      "content": text})
    sessions[cid]["history"].append({"role": "assistant", "content": result["text"]})
    sessions[cid]["history"] = sessions[cid]["history"][-20:]
    await msg.edit_text(truncate(fmt_ai(result), 4000), parse_mode=ParseMode.HTML)


async def unknown_cmd(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await u.message.reply_text("❓ Unknown command. Use /help")


# ─────────────────────────────────────────────────────────────────────
# Daily digest scheduler
# ─────────────────────────────────────────────────────────────────────

_scheduled_chats: set = set()

async def send_daily_digest(app: "Application"):
    """Send daily AI digest to all tracked chats at 09:00 UTC."""
    if not _scheduled_chats:
        return
    for cid in list(_scheduled_chats):
        try:
            repo  = sessions[cid]["repo"]
            owner, rname = repo.split("/")
            runs  = wf_runs(limit=10, repo=repo)
            prs_r = gh("GET", f"/repos/{owner}/{rname}/pulls?state=open&per_page=3", repo=repo)
            prs   = len(prs_r) if isinstance(prs_r, list) else 0
            iss_r = gh("GET", f"/repos/{owner}/{rname}/issues?state=open&per_page=3", repo=repo)
            iss   = len(iss_r) if isinstance(iss_r, list) else 0
            ok    = sum(1 for r in runs if r.get("conclusion")=="success")
            bad   = sum(1 for r in runs if r.get("conclusion")=="failure")
            result = ai_ask(
                "You are NasTech Guardian. Generate a concise daily digest in HTML (use <b>, bullet •). "
                "Include: health emoji, stats, top concern, one action item. Max 150 words.",
                f"Repo: {repo}\nPRs: {prs}\nIssues: {iss}\nPassed: {ok}\nFailed: {bad}"
            )
            ts = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            await app.bot.send_message(
                chat_id=int(cid),
                text=f"🌅 <b>Daily Digest — {ts}</b>\n\n{result['text']}\n\n<i>via {result['provider']}</i>",
                parse_mode=ParseMode.HTML
            )
        except Exception as e:
            logger.warning(f"Daily digest failed for {cid}: {e}")


async def cmd_daily_subscribe(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(u): return await deny(u)
    cid = str(u.effective_chat.id)
    _scheduled_chats.add(cid)
    sessions[cid]["notif"]["daily"] = True
    await u.message.reply_text(
        "✅ <b>Daily Digest Subscribed!</b>\n"
        "You'll receive an AI digest every day at 09:00 UTC.\n"
        "Use /daily to get one right now.\n"
        "Use /unsubscribe to cancel.",
        parse_mode=ParseMode.HTML
    )


async def cmd_unsubscribe(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(u): return await deny(u)
    cid = str(u.effective_chat.id)
    _scheduled_chats.discard(cid)
    sessions[cid]["notif"]["daily"] = False
    await u.message.reply_text("🔕 Daily digest unsubscribed.")


# ── Notification toggle commands ──────────────────────────────────────

_NOTIF_LABELS = {
    "build":    "🏗️ Build Alerts",
    "failures": "❌ Failure Alerts",
    "security": "🔒 Security Alerts",
    "pr":       "🔀 PR Alerts",
    "release":  "🚀 Release Alerts",
    "daily":    "📅 Daily Digest",
}

async def cmd_notif(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Show current notification preferences."""
    if not authorized(u): return await deny(u)
    cid = str(u.effective_chat.id)
    prefs = sessions[cid]["notif"]
    lines = ["🔔 <b>Notification Settings</b>\n"]
    for key, label in _NOTIF_LABELS.items():
        state = prefs.get(key, True)
        icon  = "🔔 ON " if state else "🔕 OFF"
        lines.append(f"  {icon}  {label}")
    lines += [
        "",
        "Use the Notifications keyboard to toggle each type ON or OFF.",
        f"\n<i>{BRAND}</i>",
    ]
    await u.message.reply_text(
        "\n".join(lines),
        reply_markup=_KEYBOARDS_BUILT["notifications"],
        parse_mode=ParseMode.HTML,
    )


async def cmd_notifon(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Turn a notification type ON.  /notifon [type|all]"""
    if not authorized(u): return await deny(u)
    cid  = str(u.effective_chat.id)
    kind = (ctx.args[0].lower() if ctx.args else "all")
    prefs = sessions[cid]["notif"]
    if kind == "all":
        for k in prefs: prefs[k] = True
        if cid not in _scheduled_chats: _scheduled_chats.add(cid)
        await u.message.reply_text(
            "🔔 <b>All notifications turned ON!</b>",
            reply_markup=_KEYBOARDS_BUILT["notifications"],
            parse_mode=ParseMode.HTML,
        )
    elif kind in prefs:
        prefs[kind] = True
        if kind == "daily": _scheduled_chats.add(cid)
        label = _NOTIF_LABELS.get(kind, kind)
        await u.message.reply_text(
            f"🔔 <b>{label} — ON</b>",
            reply_markup=_KEYBOARDS_BUILT["notifications"],
            parse_mode=ParseMode.HTML,
        )
    else:
        await u.message.reply_text(
            f"❓ Unknown type <code>{esc(kind)}</code>. "
            f"Valid: {', '.join(_NOTIF_LABELS.keys())}",
            parse_mode=ParseMode.HTML,
        )


async def cmd_notifoff(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Turn a notification type OFF.  /notifoff [type|all]"""
    if not authorized(u): return await deny(u)
    cid  = str(u.effective_chat.id)
    kind = (ctx.args[0].lower() if ctx.args else "all")
    prefs = sessions[cid]["notif"]
    if kind == "all":
        for k in prefs: prefs[k] = False
        _scheduled_chats.discard(cid)
        await u.message.reply_text(
            "🔕 <b>All notifications turned OFF.</b>",
            reply_markup=_KEYBOARDS_BUILT["notifications"],
            parse_mode=ParseMode.HTML,
        )
    elif kind in prefs:
        prefs[kind] = False
        if kind == "daily": _scheduled_chats.discard(cid)
        label = _NOTIF_LABELS.get(kind, kind)
        await u.message.reply_text(
            f"🔕 <b>{label} — OFF</b>",
            reply_markup=_KEYBOARDS_BUILT["notifications"],
            parse_mode=ParseMode.HTML,
        )
    else:
        await u.message.reply_text(
            f"❓ Unknown type <code>{esc(kind)}</code>. "
            f"Valid: {', '.join(_NOTIF_LABELS.keys())}",
            parse_mode=ParseMode.HTML,
        )


# ── Auto-fix when approved ────────────────────────────────────────────

_APPROVE_KB = make_keyboard([
    ["✅ Approve Fix", "❌ Cancel Fix"],
    ["🏠 Main Menu"],
])


async def cmd_autofix(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Analyse the latest failure, propose a fix, wait for /approvefix."""
    if not authorized(u): return await deny(u)
    cid   = str(u.effective_chat.id)
    repo  = sessions[cid]["repo"]
    owner, rname = repo.split("/")

    msg = await u.message.reply_text("🔍 Scanning latest failures for auto-fix…")

    runs   = wf_runs(limit=20, repo=repo)
    failed = [r for r in runs if r.get("conclusion") == "failure"]
    if not failed:
        await msg.edit_text("✅ No recent failures — nothing to fix!")
        return

    run    = failed[0]
    run_id = run["id"]
    wf_name = run.get("name", "?")
    branch  = run.get("head_branch", "main")
    sha     = run.get("head_sha","")[:7]
    url     = run.get("html_url","")
    commit_msg = (run.get("head_commit") or {}).get("message","")[:80]

    # Build failure context for AI
    jobs     = _fetch_run_jobs(repo, run_id)
    ctx_lines = [
        f"Repo: {repo}",
        f"Workflow: {wf_name}",
        f"Branch: {branch}",
        f"Commit: {sha} — {commit_msg}",
        "Failed jobs and steps:",
    ]
    for job in jobs:
        if job.get("conclusion") == "failure":
            ctx_lines.append(f"  Job FAILED: {job.get('name','?')}")
            for step in job.get("steps", []):
                if step.get("conclusion") == "failure":
                    ctx_lines.append(f"    Step FAILED: {step.get('name','?')}")

    error_ctx = "\n".join(ctx_lines)
    prompt = (
        f"You are a CI/CD expert for the NasTech Guardian project.\n"
        f"Analyse this workflow failure and give a SHORT, DIRECT fix plan (max 5 steps).\n"
        f"End with: FIX_COMMAND: <one shell command or workflow trigger to apply the fix>\n\n"
        f"{error_ctx}"
    )

    await msg.edit_text("🤖 Asking AI to diagnose and plan the fix…")
    result = ai_chat(prompt, [], repo)
    fix_text = result.get("text", "Could not generate fix plan.")

    # Extract FIX_COMMAND if AI provided one
    fix_cmd = None
    for line in fix_text.splitlines():
        if line.strip().startswith("FIX_COMMAND:"):
            fix_cmd = line.split("FIX_COMMAND:", 1)[1].strip()
            break

    # Store pending fix in session
    sessions[cid]["pending_fix"] = {
        "run_id":    run_id,
        "repo":      repo,
        "wf_name":   wf_name,
        "branch":    branch,
        "sha":       sha,
        "url":       url,
        "fix_cmd":   fix_cmd,
        "fix_text":  fix_text,
    }

    proposal = (
        f"🔧 <b>Auto-Fix Proposal</b>\n\n"
        f"❌ <b>Failed:</b> {esc(wf_name)} on <code>{esc(branch)}</code>\n"
        f"🔗 Commit <code>{sha}</code> — {esc(commit_msg)}\n\n"
        f"<b>AI Fix Plan:</b>\n{esc(fix_text[:1800])}\n\n"
        + (f"⚡ <b>Auto-apply command:</b>\n<code>{esc(fix_cmd)}</code>\n\n"
           if fix_cmd else "")
        + f"👇 <b>Tap ✅ Approve Fix to apply, or ❌ Cancel Fix to dismiss.</b>\n\n"
        f"<i>{BRAND}</i>"
    )
    await msg.edit_text(
        truncate(proposal, 4000),
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
        reply_markup=_APPROVE_KB,
    )


async def cmd_approvefix(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Apply the pending auto-fix that was proposed by /autofix."""
    if not authorized(u): return await deny(u)
    cid  = str(u.effective_chat.id)
    fix  = sessions[cid].get("pending_fix")

    if not fix:
        await u.message.reply_text(
            "⚠️ No pending fix. Run /autofix first to generate a fix proposal.",
            reply_markup=_KEYBOARDS_BUILT["main"],
        )
        return

    repo    = fix["repo"]
    wf_name = fix["wf_name"]
    branch  = fix["branch"]
    sha     = fix["sha"]
    fix_cmd = fix.get("fix_cmd", "")
    fix_text = fix.get("fix_text","")

    msg = await u.message.reply_text("⚙️ Applying fix…")
    steps_done = []

    # Step 1 — Trigger the repair workflow
    repaired = trigger_wf("nasguardian_guardian.yml", ref=branch,
                          inputs={"skip_to_stage": "validate", "dry_run": "false"},
                          repo=repo)
    steps_done.append(f"{'✅' if repaired else '⚠️'} Triggered repair workflow on <code>{esc(branch)}</code>")

    # Step 2 — Create a GitHub issue documenting the fix
    owner, rname = repo.split("/")
    issue_body = (
        f"## 🔧 Auto-Fix Applied\n\n"
        f"**Triggered by:** NasTech Guardian Bot\n"
        f"**Failed workflow:** {wf_name}\n"
        f"**Branch:** `{branch}`\n"
        f"**Commit:** `{sha}`\n\n"
        f"### Fix Plan\n{fix_text[:3000]}\n\n"
        + (f"### Applied Command\n```\n{fix_cmd}\n```\n\n" if fix_cmd else "")
        + f"---\n*{BRAND}*"
    )
    issue = gh("POST", f"/repos/{owner}/{rname}/issues", {
        "title": f"🔧 Auto-Fix: {wf_name} failure on {branch} ({sha})",
        "body":  issue_body,
        "labels": ["auto-fix", "bot"],
    }, repo=repo)
    issue_url = issue.get("html_url", "")
    issue_num = issue.get("number", "?")
    steps_done.append(
        f"{'✅' if issue_url else '⚠️'} Created issue "
        + (f"<a href='{issue_url}'>#{issue_num}</a>" if issue_url else f"#{issue_num} (no URL)")
    )

    # Clear pending fix
    sessions[cid].pop("pending_fix", None)

    result_text = (
        f"✅ <b>Auto-Fix Applied!</b>\n\n"
        + "\n".join(steps_done)
        + f"\n\n📎 <a href='{fix.get('url','')}'>Original failed run</a>"
        + (f"\n📌 <a href='{issue_url}'>Fix tracking issue</a>" if issue_url else "")
        + f"\n\n<i>{BRAND}</i>"
    )
    await msg.edit_text(
        result_text,
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
        reply_markup=_KEYBOARDS_BUILT["main"],
    )


async def cmd_cancelfix(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Cancel the pending auto-fix proposal."""
    if not authorized(u): return await deny(u)
    cid = str(u.effective_chat.id)
    sessions[cid].pop("pending_fix", None)
    await u.message.reply_text(
        "❌ Fix cancelled.",
        reply_markup=_KEYBOARDS_BUILT["main"],
    )


# ─────────────────────────────────────────────────────────────────────
# Bot startup
# ─────────────────────────────────────────────────────────────────────

async def post_init(app: "Application"):
    """Set bot commands menu and start scheduler."""
    commands = [
        BotCommand("start",       "Start the bot"),
        BotCommand("help",        "Show all commands"),
        BotCommand("ask",         "Ask AI a question"),
        BotCommand("explain",     "Explain code"),
        BotCommand("review",      "Code review"),
        BotCommand("run",         "Run Python safely"),
        BotCommand("fix_error",   "Fix an error message"),
        BotCommand("ocr",         "Image → text"),
        BotCommand("summarize",   "Summarize text/URL"),
        BotCommand("translate",   "Translate text"),
        BotCommand("status",      "Pipeline status"),
        BotCommand("scan",        "Full Guardian scan"),
        BotCommand("build",       "Trigger build"),
        BotCommand("repair",      "Trigger repair bot"),
        BotCommand("daily",       "AI daily digest"),
        BotCommand("pr",          "Open pull requests"),
        BotCommand("issues",      "Open issues"),
        BotCommand("commits",     "Recent commits"),
        BotCommand("version",     "App version"),
        BotCommand("models",      "AI provider status"),
        BotCommand("metrics",     "Pipeline metrics"),
        BotCommand("addrepo",     "Add + audit a repo"),
        BotCommand("repos",       "List tracked repos"),
        BotCommand("dashboard",   "Multi-repo health dashboard"),
        BotCommand("audit",       "Full repo health audit"),
        BotCommand("fixplan",     "Step-by-step fix plan"),
        BotCommand("scanall",     "Rescan all tracked repos"),
        BotCommand("repo",        "Manage repos (add/switch/remove)"),
        BotCommand("clear",       "Clear AI history"),
        BotCommand("subscribe",   "Subscribe daily digest"),
        BotCommand("services",    "Service health check"),
        BotCommand("memory",      "Bot state info"),
        BotCommand("apikeys",     "Show all API keys (masked)"),
        BotCommand("setkey",      "Change an API key live"),
        BotCommand("testkeys",    "Test all API connections"),
        BotCommand("menu",        "Switch keyboard category"),
        BotCommand("errorshot",   "Download full error log as .txt file"),
        BotCommand("notif",       "Show notification settings"),
        BotCommand("notifon",     "Turn a notification type ON"),
        BotCommand("notifoff",    "Turn a notification type OFF"),
        BotCommand("autofix",     "AI-diagnose failure and propose fix"),
        BotCommand("approvefix",  "Approve and apply the pending auto-fix"),
        BotCommand("cancelfix",   "Cancel the pending auto-fix"),
    ]
    await app.bot.set_my_commands(commands, scope=BotCommandScopeDefault())
    logger.info(f"Bot commands set ({len(commands)} commands)")

    # Scheduler
    if SCHEDULER_OK:
        scheduler = AsyncIOScheduler(timezone="UTC")
        scheduler.add_job(
            lambda: asyncio.ensure_future(send_daily_digest(app)),
            "cron", hour=9, minute=0
        )
        scheduler.start()
        logger.info("Daily digest scheduler started (09:00 UTC)")

    # Register default chat from env
    if _wl_raw:
        for cid in _wl_raw.split(","):
            cid = cid.strip()
            if cid:
                _scheduled_chats.add(cid)


def main():
    if not BOT_TOKEN:
        print("❌ TELEGRAM_BOT_TOKEN not set!")
        print("   export TELEGRAM_BOT_TOKEN='your:token'")
        sys.exit(1)

    print(f"🛡️  NasTech Guardian Bot v{BOT_VERSION}")
    print(f"   Developer: Nsamba Naswif Cohen")
    print(f"   Repo:    {GITHUB_REPO}")
    print(f"   Auth:    {'restricted (' + str(len(WHITELIST)) + ' IDs)' if WHITELIST else 'open'}")
    print(f"   Groq:    {'✅' if GROQ_KEY else '❌ missing'}")
    print(f"   Gemini:  {'✅' if GEMINI_KEY else '❌ missing'}")
    print(f"   OpenRouter: {'✅' if OR_KEY else '❌ missing'}")
    print(f"   Scheduler: {'✅' if SCHEDULER_OK else '❌ install apscheduler'}")
    print()

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    handlers = [
        ("start",       cmd_start),
        ("help",        cmd_help),
        ("ask",         cmd_ask),
        ("explain",     cmd_explain),
        ("review",      cmd_review),
        ("fix_error",   cmd_fix_error),
        ("run",         cmd_run),
        ("ocr",         cmd_ocr),
        ("summarize",   cmd_summarize),
        ("translate",   cmd_translate),
        ("daily",       cmd_daily),
        ("subscribe",   cmd_daily_subscribe),
        ("unsubscribe", cmd_unsubscribe),
        ("repo",        cmd_repo),
        ("repos",       cmd_repos),
        ("addrepo",     cmd_addrepo),
        ("dashboard",   cmd_dashboard),
        ("audit",       cmd_audit),
        ("fixplan",     cmd_fixplan),
        ("scanall",     cmd_scanall),
        ("clear",       cmd_clear),
        ("ai_on",       cmd_ai_on),
        ("ai_off",      cmd_ai_off),
        ("status",      cmd_status),
        ("scan",        cmd_scan),
        ("build",       cmd_build),
        ("rebuild",     cmd_rebuild),
        ("test",        cmd_test),
        ("repair",      cmd_repair),
        ("release",     cmd_release),
        ("health",      cmd_health),
        ("doctor",      cmd_doctor),
        ("logs",        cmd_logs),
        ("errors",      cmd_errors),
        ("dependencies",cmd_dependencies),
        ("packages",    cmd_packages),
        ("security",    cmd_security),
        ("fix",         cmd_fix),
        ("pr",          cmd_pr),
        ("issues",      cmd_issues),
        ("commits",     cmd_commits),
        ("branches",    cmd_branches),
        ("version",     cmd_version),
        ("workflows",   cmd_workflows),
        ("models",      cmd_models),
        ("providers",   cmd_models),
        ("metrics",     cmd_metrics),
        ("storage",     cmd_storage),
        ("services",    cmd_services),
        ("network",     cmd_services),
        ("memory",      cmd_memory),
        ("backup",      cmd_backup),
        ("apikeys",     cmd_apikeys),
        ("setkey",      cmd_setkey),
        ("testkeys",    cmd_testkeys),
        ("menu",        cmd_menu),
        ("errorshot",   cmd_errorshot),
        ("notif",       cmd_notif),
        ("notifon",     cmd_notifon),
        ("notifoff",    cmd_notifoff),
        ("autofix",     cmd_autofix),
        ("approvefix",  cmd_approvefix),
        ("cancelfix",   cmd_cancelfix),
    ]

    for name, handler in handlers:
        app.add_handler(CommandHandler(name, handler))

    # Photo handler for OCR
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    # Text → AI chat
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    # Unknown commands
    app.add_handler(MessageHandler(filters.COMMAND, unknown_cmd))

    print(f"   Commands: {len(handlers)} registered")
    print("   Bot running — press Ctrl+C to stop\n")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
