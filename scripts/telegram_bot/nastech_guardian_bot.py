#!/usr/bin/env python3
"""
NasTech Guardian Telegram Bot v3.0
Multi-repo + full audit + fix-plan + TeleBotList features:
  - AI chat with session history (Groq → Gemini → OpenRouter)
  - /ask /explain /review /fix_error /run /ocr /summarize /translate
  - Multi-repo: /repo add|list|switch|audit|remove, /dashboard, /audit, /fixplan
  - Full pre-join audit: secrets, workflows, builds, issues, security
  - Per-repo fix plans with prioritised step-by-step instructions
  - 50+ CI/CD commands, daily digest, OCR, group management

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
        BotCommand, BotCommandScopeDefault
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
GITHUB_REPO  = os.environ.get("GITHUB_REPO", "nastech-ai/NasTerminal")
GUARDIAN_WF  = "nastech_guardian.yml"
BOT_VERSION  = "2.0.0"

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
sessions: dict = defaultdict(lambda: {
    "history":     [],   # AI conversation history
    "repo":        GITHUB_REPO,
    "ai_mode":     True,
    "last_active": 0.0,
})


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


# ─────────────────────────────────────────────────────────────────────
# Command handlers
# ─────────────────────────────────────────────────────────────────────

async def cmd_start(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(u): return await deny(u)
    cid = str(u.effective_chat.id)
    await u.message.reply_text(
        f"🛡️ <b>NasTech Guardian Bot v{BOT_VERSION}</b>\n"
        f"Repo: <code>{sessions[cid]['repo']}</code>\n\n"
        "I am your AI DevOps assistant + CI/CD orchestrator.\n"
        "Just <b>type any message</b> to chat with AI, or use /help for all commands.",
        parse_mode=ParseMode.HTML
    )


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
        "<i>Or just type a message to chat with AI!</i>",
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
    cid  = str(u.effective_chat.id)
    repo = sessions[cid]["repo"]
    msg  = await u.message.reply_text("⏳ Triggering build…")
    ok   = trigger_wf("debug_build.yml", repo=repo)
    await msg.edit_text(
        ('🔨 <b>Build triggered!</b>' if ok else '❌ Failed.') +
        f"\n<a href='https://github.com/{esc(repo)}/actions'>Monitor</a>",
        parse_mode=ParseMode.HTML, disable_web_page_preview=True
    )


async def cmd_rebuild(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(u): return await deny(u)
    cid  = str(u.effective_chat.id)
    repo = sessions[cid]["repo"]
    ok   = trigger_wf(GUARDIAN_WF, inputs={"force_repair": "false"}, repo=repo)
    await u.message.reply_text(
        f"{'🔨 <b>Rebuild triggered!</b>' if ok else '❌ Failed.'}",
        parse_mode=ParseMode.HTML
    )


async def cmd_test(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(u): return await deny(u)
    cid  = str(u.effective_chat.id)
    repo = sessions[cid]["repo"]
    ok   = trigger_wf("run_tests.yml", repo=repo)
    await u.message.reply_text(f"{'🧪 <b>Tests triggered!</b>' if ok else '❌ Failed.'}", parse_mode=ParseMode.HTML)


async def cmd_repair(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(u): return await deny(u)
    cid  = str(u.effective_chat.id)
    repo = sessions[cid]["repo"]
    ok   = trigger_wf(GUARDIAN_WF, inputs={"force_repair": "true", "dry_run": "false"}, repo=repo)
    await u.message.reply_text(
        ('🔧 <b>Repair Bot triggered!</b>\nA PR will be created if patches are found.' if ok else '❌ Failed.'),
        parse_mode=ParseMode.HTML
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
    ok   = trigger_wf(GUARDIAN_WF, repo=repo)
    await u.message.reply_text(
        ('🏥 <b>Health check triggered!</b>' if ok else '❌ Failed.') +
        f"\n<a href='https://github.com/{esc(repo)}/actions'>Monitor</a>",
        parse_mode=ParseMode.HTML, disable_web_page_preview=True
    )


async def cmd_doctor(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(u): return await deny(u)
    cid  = str(u.effective_chat.id)
    repo = sessions[cid]["repo"]
    ok   = trigger_wf("nastech_doctor.yml", repo=repo)
    await u.message.reply_text(
        f"{'🩺 <b>Doctor scan triggered!</b>' if ok else '❌ Failed.'}",
        parse_mode=ParseMode.HTML
    )


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


async def cmd_errors(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(u): return await deny(u)
    cid  = str(u.effective_chat.id)
    repo = sessions[cid]["repo"]
    msg  = await u.message.reply_text("⏳ Checking for failures…")
    runs = wf_runs(limit=15, repo=repo)
    failed = [r for r in runs if r.get("conclusion") == "failure"]
    if not failed:
        await msg.edit_text("✅ No recent failures!")
        return
    lines = [f"❌ <b>Recent Failures ({len(failed)})</b>\n"]
    for r in failed[:5]:
        sha  = r.get("head_sha","")[:7]
        name = r.get("name","?")[:30]
        url  = r.get("html_url","")
        ts   = r.get("created_at","")[:16]
        lines.append(f"❌ <code>{sha}</code> {esc(name)} ({ts})\n<a href='{url}'>View</a>")
    await msg.edit_text("\n".join(lines), parse_mode=ParseMode.HTML,
                        disable_web_page_preview=True)


async def cmd_dependencies(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(u): return await deny(u)
    cid = str(u.effective_chat.id)
    repo = sessions[cid]["repo"]
    ok   = trigger_wf("nastech_audit.yml", repo=repo)
    await u.message.reply_text(
        f"{'📦 <b>Dependency audit triggered!</b>' if ok else '❌ Failed.'}",
        parse_mode=ParseMode.HTML
    )


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
    ok   = trigger_wf("nastech_doctor.yml", repo=repo)
    await u.message.reply_text(
        f"{'🔒 <b>Security scan triggered!</b>' if ok else '❌ Failed.'}",
        parse_mode=ParseMode.HTML
    )


async def cmd_fix(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(u): return await deny(u)
    cid  = str(u.effective_chat.id)
    repo = sessions[cid]["repo"]
    ok   = trigger_wf(GUARDIAN_WF, inputs={"force_repair":"true"}, repo=repo)
    await u.message.reply_text(
        ('🔧 <b>Fix PR generation triggered!</b>\nCheck /pr when complete.' if ok else '❌ Failed.'),
        parse_mode=ParseMode.HTML
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
        return  # Let command handlers deal with it
    cid = str(u.effective_chat.id)
    if not sessions[cid]["ai_mode"]:
        return
    # Rate limit: 2 seconds between AI requests
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
    await u.message.reply_text("🔕 Daily digest unsubscribed.")


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
