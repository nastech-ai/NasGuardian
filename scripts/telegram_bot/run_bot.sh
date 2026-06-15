#!/usr/bin/env bash
# NasTech Guardian Bot Runner
# Usage: bash scripts/telegram_bot/run_bot.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

echo "🛡️  NasTech Guardian Bot"
echo "   Script: $SCRIPT_DIR/nastech_guardian_bot.py"
echo "   Repo:   $REPO_ROOT"
echo ""

# ── Check required secrets ───────────────────────────────────────
check_var() {
  if [ -z "${!1:-}" ]; then
    echo "❌ Missing required env var: $1"
    echo "   Set it with: export $1='value'"
    return 1
  fi
  echo "✅ $1 is set"
}

MISSING=0
check_var TELEGRAM_BOT_TOKEN || MISSING=1
check_var GITHUB_TOKEN        || MISSING=1

if [ "$MISSING" = "1" ]; then
  echo ""
  echo "Required secrets missing. Aborting."
  exit 1
fi

# ── Optional secrets check ───────────────────────────────────────
for v in GROQ_API_KEY GEMINI_API_KEY OPENROUTER_API_KEY TELEGRAM_CHAT_ID; do
  if [ -z "${!v:-}" ]; then
    echo "⚠️  Optional: $v not set (some AI commands may be limited)"
  else
    echo "✅ $v is set"
  fi
done

echo ""

# ── Install deps ─────────────────────────────────────────────────
if ! python3 -c "import telegram" 2>/dev/null; then
  echo "📦 Installing bot dependencies..."
  pip install -r "$SCRIPT_DIR/requirements.txt" --quiet
fi

# ── Set GITHUB_REPO default ──────────────────────────────────────
if [ -z "${GITHUB_REPO:-}" ]; then
  # Try to detect from git remote
  REMOTE=$(git -C "$REPO_ROOT" remote get-url origin 2>/dev/null || echo "")
  if echo "$REMOTE" | grep -qE "github\.com[:/](.+/.+)(\.git)?$"; then
    GITHUB_REPO=$(echo "$REMOTE" | sed -E 's|.*github\.com[:/](.+/.+)(\.git)?$|\1|')
    export GITHUB_REPO
    echo "🔍 Auto-detected repo: $GITHUB_REPO"
  else
    export GITHUB_REPO="nastech-ai/NasTerminal"
    echo "ℹ️  Using default repo: $GITHUB_REPO"
  fi
fi

echo ""
echo "🚀 Starting NasTech Guardian Bot..."
echo "   Repo: $GITHUB_REPO"
echo "   Press Ctrl+C to stop."
echo ""

exec python3 "$SCRIPT_DIR/nastech_guardian_bot.py"
