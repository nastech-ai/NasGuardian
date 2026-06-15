#!/usr/bin/env python3
"""
NasTech Guardian — AI Coordinator
Routes analysis tasks to Groq → Gemini → OpenRouter with automatic fallback.
Each provider is tried in priority order. Falls back on timeout or error.
"""

import json
import os
import sys
import time
import urllib.request
import urllib.error
from typing import Optional


PROVIDERS = ["groq", "gemini", "openrouter"]

GROQ_MODELS = [
    "llama-3.3-70b-versatile",
    "llama-3.1-8b-instant",
    "mixtral-8x7b-32768",
]

GEMINI_MODELS = [
    "gemini-2.0-flash",
    "gemini-1.5-pro",
]

OPENROUTER_MODELS = [
    "meta-llama/llama-3.3-70b-instruct:free",
    "google/gemini-flash-1.5:free",
    "mistralai/mistral-7b-instruct:free",
]

TIMEOUT = 30  # seconds per request


def _http_post(url: str, headers: dict, body: dict) -> dict:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code}: {e.read().decode('utf-8', errors='replace')[:300]}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"Network error: {e.reason}")


def ask_groq(prompt: str, system: str = "", model: str = None) -> str:
    """Query Groq API."""
    api_key = os.environ.get("GROQ_API_KEY", "")
    if not api_key:
        raise RuntimeError("GROQ_API_KEY not set")

    model = model or GROQ_MODELS[0]
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    result = _http_post(
        "https://api.groq.com/openai/v1/chat/completions",
        headers={
            "Content-Type":  "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        body={
            "model":       model,
            "messages":    messages,
            "max_tokens":  2048,
            "temperature": 0.1,
        },
    )
    return result["choices"][0]["message"]["content"].strip()


def ask_gemini(prompt: str, system: str = "", model: str = None) -> str:
    """Query Google Gemini API."""
    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY not set")

    model = model or GEMINI_MODELS[0]
    full_prompt = f"{system}\n\n{prompt}" if system else prompt

    result = _http_post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}",
        headers={"Content-Type": "application/json"},
        body={
            "contents": [{"parts": [{"text": full_prompt}]}],
            "generationConfig": {"maxOutputTokens": 2048, "temperature": 0.1},
        },
    )
    return result["candidates"][0]["content"]["parts"][0]["text"].strip()


def ask_openrouter(prompt: str, system: str = "", model: str = None) -> str:
    """Query OpenRouter API."""
    api_key = os.environ.get("OPENROUTER_API_KEY", "")
    if not api_key:
        raise RuntimeError("OPENROUTER_API_KEY not set")

    model = model or OPENROUTER_MODELS[0]
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    result = _http_post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers={
            "Content-Type":  "application/json",
            "Authorization": f"Bearer {api_key}",
            "HTTP-Referer":  "https://github.com/nastech-ai",
            "X-Title":       "NasTech Guardian",
        },
        body={
            "model":       model,
            "messages":    messages,
            "max_tokens":  2048,
            "temperature": 0.1,
        },
    )
    return result["choices"][0]["message"]["content"].strip()


_PROVIDER_FNS = {
    "groq":        ask_groq,
    "gemini":      ask_gemini,
    "openrouter":  ask_openrouter,
}


def analyze(
    prompt: str,
    system: str = "You are NasTech Guardian, an expert Android/Termux build engineer. "
                  "Respond with concise, actionable JSON.",
    providers: list = None,
    task_type: str = "general",
) -> dict:
    """
    Send a prompt to AI providers in priority order.
    Returns: {"response": str, "provider": str, "model": str, "attempts": list}

    Task types influence model selection:
      - "security"     → use strongest model
      - "repair"       → use code-capable model
      - "analysis"     → fast model OK
      - "general"      → default
    """
    providers = providers or PROVIDERS
    attempts = []

    for provider in providers:
        fn = _PROVIDER_FNS.get(provider)
        if fn is None:
            continue

        try:
            t0 = time.time()
            response = fn(prompt, system)
            elapsed = round(time.time() - t0, 2)
            attempts.append({"provider": provider, "status": "success", "elapsed": elapsed})
            return {
                "response":  response,
                "provider":  provider,
                "elapsed":   elapsed,
                "attempts":  attempts,
                "task_type": task_type,
            }
        except Exception as e:
            attempts.append({"provider": provider, "status": "error", "error": str(e)})
            print(f"[AI Coordinator] {provider} failed: {e}", file=sys.stderr)
            continue

    # All providers failed — return structured error
    return {
        "response":  None,
        "provider":  None,
        "error":     "All AI providers unavailable",
        "attempts":  attempts,
        "task_type": task_type,
    }


def analyze_build_failure(build_log: str, context: dict = None) -> dict:
    """Specialized: analyze a build failure and suggest fixes."""
    system = (
        "You are NasTech Guardian Build Expert. Analyze this Android/Gradle build failure. "
        "Return ONLY valid JSON: {\"root_cause\": str, \"fix_steps\": [str], "
        "\"file_patches\": [{\"file\": str, \"search\": str, \"replace\": str}], "
        "\"confidence\": 0-100}"
    )
    prompt = f"BUILD LOG:\n```\n{build_log[:3000]}\n```\n"
    if context:
        prompt += f"\nCONTEXT: {json.dumps(context)[:1000]}"
    return analyze(prompt, system, task_type="repair")


def analyze_security(findings: list) -> dict:
    """Specialized: security findings analysis."""
    system = (
        "You are NasTech Guardian Security Expert. Analyze these Android security findings. "
        "Return ONLY valid JSON: {\"severity\": 'critical|high|medium|low', "
        "\"findings\": [{\"issue\": str, \"file\": str, \"fix\": str}], "
        "\"overall_risk\": str}"
    )
    prompt = f"SECURITY FINDINGS:\n{json.dumps(findings, indent=2)[:2000]}"
    return analyze(prompt, system, task_type="security")


def analyze_dependencies(dep_report: dict) -> dict:
    """Specialized: dependency issue analysis."""
    system = (
        "You are NasTech Guardian Dependency Expert. Analyze these dependency issues. "
        "Return ONLY valid JSON: {\"critical_issues\": [str], "
        "\"fix_commands\": [str], \"upgrade_suggestions\": [{\"dep\": str, \"from\": str, \"to\": str}]}"
    )
    prompt = f"DEPENDENCY REPORT:\n{json.dumps(dep_report, indent=2)[:2000]}"
    return analyze(prompt, system, task_type="analysis")


def generate_changelog(commits: list, version: str) -> str:
    """Generate a changelog from commit messages."""
    system = (
        "You are NasTech Guardian Release Expert. Generate a clean changelog. "
        "Format: markdown with sections: Features, Fixes, Dependencies, Other."
    )
    commit_list = "\n".join(f"- {c}" for c in commits[:50])
    prompt = f"VERSION: {version}\nCOMMITS:\n{commit_list}"
    result = analyze(prompt, system, task_type="general")
    return result.get("response") or "## Changelog\n\n- Automated release"


def chat(user_message: str, history: list = None) -> dict:
    """
    General-purpose conversational AI for the Telegram bot.
    history: [{"role": "user"|"assistant", "content": str}, ...]
    Returns: {"response": str, "provider": str, "ok": bool}
    """
    system = (
        "You are NasTech Guardian, an expert AI DevOps assistant for the NasTech AI Terminal "
        "(a Termux-based Android app). You help with: Android builds, Gradle, GitHub Actions, "
        "Python scripting, CI/CD pipelines, and Telegram bot development. "
        "Be concise and actionable. Use code blocks for commands/code."
    )
    context = ""
    if history:
        for h in history[-6:]:
            prefix = "User" if h["role"] == "user" else "Assistant"
            context += f"{prefix}: {h['content']}\n"
    prompt = f"{context}User: {user_message}" if context else user_message
    result = analyze(prompt, system, task_type="general")
    return {
        "response": result.get("response") or "Sorry, all AI providers are currently unavailable.",
        "provider": result.get("provider"),
        "ok":       result.get("response") is not None,
    }


def explain_code(code: str, language: str = "auto") -> dict:
    """Explain what a code snippet does."""
    system = (
        "You are a code explainer. Given a code snippet, explain:\n"
        "1. What it does (2-3 sentences)\n"
        "2. Key functions/patterns used\n"
        "3. Potential issues or improvements (bullet list)\n"
        "Be concise. Use code blocks for examples."
    )
    prompt = f"Language: {language}\n\n```{language}\n{code[:3000]}\n```"
    result = analyze(prompt, system, task_type="analysis")
    return {
        "response": result.get("response") or "Could not explain code.",
        "provider": result.get("provider"),
        "ok":       result.get("response") is not None,
    }


def review_code(code: str, language: str = "auto") -> dict:
    """Code review: bugs, security, style."""
    system = (
        "You are a senior code reviewer. Review the code for:\n"
        "- Bugs and logic errors\n"
        "- Security vulnerabilities\n"
        "- Performance issues\n"
        "- Style and best practices\n"
        "Format as a numbered list. Include severity (🔴/🟡/🟢) per item."
    )
    prompt = f"Language: {language}\n\n```{language}\n{code[:3000]}\n```"
    result = analyze(prompt, system, task_type="security")
    return {
        "response": result.get("response") or "Could not review code.",
        "provider": result.get("provider"),
        "ok":       result.get("response") is not None,
    }


def suggest_fix(error_message: str, code_context: str = "") -> dict:
    """Diagnose an error and suggest a fix."""
    system = (
        "You are a debugging assistant. Given an error message, provide:\n"
        "1. Root cause (1 sentence)\n"
        "2. Fix (with corrected code if applicable)\n"
        "3. Prevention tip\n"
        "Be concise. Use code blocks."
    )
    prompt = f"Error:\n```\n{error_message[:1000]}\n```"
    if code_context:
        prompt += f"\n\nCode context:\n```\n{code_context[:2000]}\n```"
    result = analyze(prompt, system, task_type="repair")
    return {
        "response": result.get("response") or "Could not suggest a fix.",
        "provider": result.get("provider"),
        "ok":       result.get("response") is not None,
    }


def daily_summary(repo: str, recent_runs: list, open_prs: int, open_issues: int) -> dict:
    """Generate a daily digest summary."""
    system = (
        "You are NasTech Guardian. Generate a concise daily digest in Markdown. "
        "Include: overall health (emoji), key metrics, any action items. Max 200 words."
    )
    prompt = (
        f"Repository: {repo}\n"
        f"Open PRs: {open_prs}\n"
        f"Open Issues: {open_issues}\n"
        f"Recent workflow runs: {json.dumps(recent_runs[:5], indent=2)[:800]}"
    )
    result = analyze(prompt, system, task_type="general")
    return {
        "response": result.get("response") or "Daily summary unavailable.",
        "provider": result.get("provider"),
        "ok":       result.get("response") is not None,
    }


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="NasTech AI Coordinator")
    parser.add_argument("--prompt", required=True)
    parser.add_argument("--system", default="")
    parser.add_argument("--task",   default="general")
    parser.add_argument("--json-output", action="store_true")
    parser.add_argument("--self-test",   action="store_true")
    args = parser.parse_args()

    if args.self_test:
        print("🤖 NasTech AI Coordinator — Provider Self-Test")
        print(f"  Groq key:        {'✅ set' if os.environ.get('GROQ_API_KEY') else '❌ missing'}")
        print(f"  Gemini key:      {'✅ set' if os.environ.get('GEMINI_API_KEY') else '❌ missing'}")
        print(f"  OpenRouter key:  {'✅ set' if os.environ.get('OPENROUTER_API_KEY') else '❌ missing'}")
        print()
        result = chat("Hello! Briefly describe NasTech Guardian in one sentence.")
        print(f"  Provider: {result['provider']}")
        print(f"  Response: {result['response'][:200]}")
        sys.exit(0 if result["ok"] else 1)

    result = analyze(args.prompt, args.system, task_type=args.task)
    if args.json_output:
        print(json.dumps(result, indent=2))
    else:
        print(result.get("response") or f"ERROR: {result.get('error')}")
        print(f"\n[Used: {result.get('provider', 'none')}]", file=sys.stderr)
