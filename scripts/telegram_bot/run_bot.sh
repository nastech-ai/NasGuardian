#!/usr/bin/env bash
# NasTech Guardian Bot — Launcher
# Usage: bash scripts/telegram_bot/run_bot.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(dirname "$(dirname "$SCRIPT_DIR")")"
BOT_SCRIPT="$SCRIPT_DIR/nastech_guardian_bot.py"
ENV_FILE="$HOME/.nastech_env"
LOG_FILE="$HOME/nastech_guardian_bot.log"

R='\033[0;31m'; G='\033[0;32m'; Y='\033[1;33m'; C='\033[0;36m'; N='\033[0m'; BOLD='\033[1m'

echo ""
echo -e "${BOLD}${C}╔══════════════════════════════════════════════════╗${N}"
echo -e "${BOLD}${C}║  🛡️  NasTech Guardian Bot — Launcher             ║${N}"
echo -e "${BOLD}${C}╚══════════════════════════════════════════════════╝${N}"
echo ""

# Load env if available
if [ -f "$ENV_FILE" ]; then
    source "$ENV_FILE"
    echo -e "${G}✓${N} Loaded env from $ENV_FILE"
fi

# Map GITHUB_PERSONAL_ACCESS_TOKEN → GITHUB_TOKEN / GH_TOKEN
if [ -n "${GITHUB_PERSONAL_ACCESS_TOKEN:-}" ]; then
    export GITHUB_TOKEN="${GITHUB_TOKEN:-$GITHUB_PERSONAL_ACCESS_TOKEN}"
    export GH_TOKEN="${GH_TOKEN:-$GITHUB_PERSONAL_ACCESS_TOKEN}"
fi

# Check required env
MISSING=0
for key in TELEGRAM_BOT_TOKEN TELEGRAM_CHAT_ID; do
    if [ -z "${!key:-}" ]; then
        echo -e "${R}❌ Missing: $key${N}"
        MISSING=1
    else
        echo -e "${G}✅ $key: set${N}"
    fi
done

for key in GROQ_API_KEY GEMINI_API_KEY OPENROUTER_API_KEY GITHUB_TOKEN; do
    if [ -n "${!key:-}" ]; then
        echo -e "${G}✅ $key: set${N}"
    else
        echo -e "${Y}⚠️  $key: not set (optional — some features disabled)${N}"
    fi
done

echo ""

if [ "$MISSING" -eq 1 ]; then
    echo -e "${R}❌ Cannot start bot — required secrets missing.${N}"
    echo -e "   Set them via: bash scripts/telegram_bot/run_bot.sh"
    echo -e "   Or run: python3 run_bot.py in the project root"
    exit 1
fi

# Install dependencies if needed
if ! python3 -c "import telegram" 2>/dev/null; then
    echo -e "${Y}[→]${N} Installing dependencies…"
    pip install -r "$SCRIPT_DIR/requirements.txt" --quiet
fi

echo -e "${G}[→]${N} Starting NasTech Guardian Bot…"
echo -e "    Log: $LOG_FILE"
echo ""

cd "$ROOT_DIR"
exec python3 "$BOT_SCRIPT" 2>&1 | tee -a "$LOG_FILE"
