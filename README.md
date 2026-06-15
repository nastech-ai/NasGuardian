# 🛡️ NasTech Guardian

**Multi-agent CI/CD orchestration system for Android/Termux apps**

> Companion to [NasTech AI Terminal](https://github.com/nastech-ai/NasTerminal)

---

## What it does

- 🤖 **7 specialized AI agents** — identity, dependency, health, build, repair, release, notify
- 📱 **Telegram bot** with 50+ commands — AI chat, CI/CD control, multi-repo management
- 🔍 **Multi-repo auditor** — 15-point health check before adding any repo
- 🤖 **AI coordinator** — Groq → Gemini → OpenRouter fallback chain
- 📱 **Termux installer** — always-live bot on Android with tmux + termux-boot
- 🔄 **Daily digest** — AI-generated pipeline status every morning at 09:00 UTC

## Quick Start (Termux / Android)

```bash
pkg install git -y
git clone https://github.com/nastech-ai/NasGuardian ~/nastech-guardian
cd ~/nastech-guardian
bash scripts/termux_install.sh
```

## Telegram Bot Commands

| Category | Commands |
|---|---|
| AI Chat | `/ask` `/explain` `/review` `/run` `/fix_error` |
| Repos | `/addrepo` `/repos` `/dashboard` `/audit` `/fixplan` `/scanall` |
| Pipeline | `/status` `/scan` `/build` `/repair` `/release` `/health` |
| Analysis | `/logs` `/errors` `/dependencies` `/security` `/metrics` |
| Tools | `/ocr` `/summarize` `/translate` `/daily` |

## Required Secrets

Set these in GitHub Actions:

```
TELEGRAM_BOT_TOKEN
TELEGRAM_CHAT_ID
GROQ_API_KEY
GEMINI_API_KEY
OPENROUTER_API_KEY
GITHUB_PERSONAL_ACCESS_TOKEN
```

## Architecture

```
Guardian Pipeline
IDLE → VERIFY → IDENTIFY → SCAN → BUILD → FIX → VERIFY → RELEASE → NOTIFY → COMPLETE
       identity   dependency  health  build    repair          release    notify
```

## License

Apache-2.0 — see [NasTerminal](https://github.com/nastech-ai/NasTerminal)
