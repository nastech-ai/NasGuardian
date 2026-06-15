#!/usr/bin/env bash
# ════════════════════════════════════════════════════════════════════════
#  NasTech Guardian — Bundle Creator
#  Creates a self-contained ZIP for Termux / Android install
#  Usage: bash scripts/create_bundle.sh [version]
# ════════════════════════════════════════════════════════════════════════
set -euo pipefail

VERSION="${1:-3.0}"
BUNDLE_NAME="nastech-guardian-v${VERSION}"
BUNDLE_DIR="/tmp/${BUNDLE_NAME}"
OUTPUT_ZIP="${BUNDLE_NAME}.zip"

R='\033[0;31m'; G='\033[0;32m'; Y='\033[1;33m'
B='\033[0;34m'; C='\033[0;36m'; N='\033[0m'; BOLD='\033[1m'

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"

echo ""
echo -e "${BOLD}${C}╔══════════════════════════════════════════════════╗${N}"
echo -e "${BOLD}${C}║  NasTech Guardian — Bundle Creator v${VERSION}           ║${N}"
echo -e "${BOLD}${C}╚══════════════════════════════════════════════════╝${N}"
echo ""
echo -e "${B}[→]${N} Source: ${ROOT_DIR}"
echo -e "${B}[→]${N} Output: ${OUTPUT_ZIP}"
echo ""

# ── Clean up ──────────────────────────────────────────────────────────
rm -rf "${BUNDLE_DIR}" "${OUTPUT_ZIP}" 2>/dev/null || true
mkdir -p "${BUNDLE_DIR}"

# ── Copy core files ───────────────────────────────────────────────────
echo -e "${B}[→]${N} Copying files…"

copy_file() {
    local src="$1"
    local dst_rel="$2"
    local dst="${BUNDLE_DIR}/${dst_rel}"
    if [[ -f "${ROOT_DIR}/${src}" ]]; then
        mkdir -p "$(dirname "${dst}")"
        cp "${ROOT_DIR}/${src}" "${dst}"
        echo -e "  ${G}✓${N} ${dst_rel}"
    else
        echo -e "  ${Y}!${N} skip (not found): ${src}"
    fi
}

# Termux installer (most important — run this first)
copy_file "scripts/termux_install.sh"            "termux_install.sh"

# Guardian agents
copy_file "scripts/guardian/__init__.py"          "scripts/guardian/__init__.py"
copy_file "scripts/guardian/ai_coordinator.py"    "scripts/guardian/ai_coordinator.py"
copy_file "scripts/guardian/identity_bot.py"      "scripts/guardian/identity_bot.py"
copy_file "scripts/guardian/dependency_bot.py"    "scripts/guardian/dependency_bot.py"
copy_file "scripts/guardian/health_bot.py"        "scripts/guardian/health_bot.py"
copy_file "scripts/guardian/build_bot.py"         "scripts/guardian/build_bot.py"
copy_file "scripts/guardian/repair_bot.py"        "scripts/guardian/repair_bot.py"
copy_file "scripts/guardian/release_bot.py"       "scripts/guardian/release_bot.py"
copy_file "scripts/guardian/notify_bot.py"        "scripts/guardian/notify_bot.py"
copy_file "scripts/guardian/state_machine.py"     "scripts/guardian/state_machine.py"
copy_file "scripts/guardian/repo_manager.py"      "scripts/guardian/repo_manager.py"
copy_file "scripts/guardian/requirements.txt"     "scripts/guardian/requirements.txt"

# Telegram bot
copy_file "scripts/telegram_bot/__init__.py"                      "scripts/telegram_bot/__init__.py"
copy_file "scripts/telegram_bot/nastech_guardian_bot.py"          "scripts/telegram_bot/nastech_guardian_bot.py"
copy_file "scripts/telegram_bot/requirements.txt"                 "scripts/telegram_bot/requirements.txt"
copy_file "scripts/telegram_bot/run_bot.sh"                       "scripts/telegram_bot/run_bot.sh"

# Workflows
copy_file ".github/workflows/nastech_guardian.yml"   "github-workflows/nastech_guardian.yml"
copy_file ".github/workflows/nastech_bot.yml"         "github-workflows/nastech_bot.yml"
copy_file ".github/workflows/nastech_pr_manager.yml" "github-workflows/nastech_pr_manager.yml"
copy_file ".github/workflows/nastech_audit.yml"      "github-workflows/nastech_audit.yml"
copy_file ".github/workflows/nastech_doctor.yml"     "github-workflows/nastech_doctor.yml"

# ── Generate README.md ────────────────────────────────────────────────
cat > "${BUNDLE_DIR}/README.md" << 'READMEEOF'
# 🛡️ NasTech Guardian v3.0

Multi-agent CI/CD orchestration for Android + Termux.

## Quick Install (Termux on Android)

```bash
# Option A — from GitHub (recommended)
pkg install git -y
git clone https://github.com/nastech-ai/NasGuardian ~/nastech-guardian
cd ~/nastech-guardian
bash termux_install.sh

# Option B — from this ZIP bundle
unzip nastech-guardian-v3.0.zip -d ~/nastech-guardian
cd ~/nastech-guardian
bash termux_install.sh
```

## What gets installed

- Python 3 + all bot dependencies
- Telegram bot running in tmux (always-live)
- Auto-start via Termux:Boot on device reboot
- `nastech-start` / `nastech-stop` / `nastech-status` / `nastech-logs` aliases

## Telegram Bot Commands

| Category | Commands |
|---|---|
| 🔑 API Keys | `/apikeys` `/setkey KEY value` `/testkeys` |
| 🤖 AI Chat | `/ask` `/explain` `/review` `/run` `/fix_error` |
| 🔍 Repos | `/addrepo` `/repos` `/dashboard` `/audit` `/scanall` |
| ⚙️ Pipeline | `/status` `/scan` `/build` `/repair` `/release` `/health` |
| 🛠️ Tools | `/ocr` `/summarize` `/translate` `/daily` |

## Change API Keys via Telegram

```
/apikeys              — show all keys (masked)
/setkey GROQ_API_KEY gsk_xxx       — update Groq key
/setkey GEMINI_API_KEY AIzaxx      — update Gemini key
/setkey OPENROUTER_API_KEY sk-xx   — update OpenRouter key
/setkey GITHUB_TOKEN ghp_xxx       — update GitHub PAT
/testkeys             — test all API connections
```

## Required Secrets

```
TELEGRAM_BOT_TOKEN
TELEGRAM_CHAT_ID
GROQ_API_KEY
GEMINI_API_KEY
OPENROUTER_API_KEY
GITHUB_TOKEN   (ghp_... personal access token)
```

## Links

- 🤖 Bot: @Nightscafebot
- 📱 NasTerminal: https://github.com/nastech-ai/NasTerminal
- 🛡️ NasGuardian: https://github.com/nastech-ai/NasGuardian
READMEEOF
echo -e "  ${G}✓${N} README.md"

# ── Generate nastech_config.sh (standalone key manager) ──────────────
cat > "${BUNDLE_DIR}/nastech_config.sh" << 'CONFIGEOF'
#!/usr/bin/env bash
# NasTech Config — API Key Manager
# Usage: bash nastech_config.sh [set KEY value | show | test]

ENV_FILE="$HOME/.nastech_env"

R='\033[0;31m'; G='\033[0;32m'; Y='\033[1;33m'; N='\033[0m'; BOLD='\033[1m'

mask_val() {
    local v="$1"
    [[ -z "$v" ]] && echo "❌ not set" && return
    echo "✅ ${v:0:6}…${v: -3}"
}

load_env() {
    [[ -f "$ENV_FILE" ]] && source "$ENV_FILE" 2>/dev/null || true
}

save_key() {
    local key="$1" val="$2"
    touch "$ENV_FILE"
    if grep -q "^export ${key}=" "$ENV_FILE" 2>/dev/null; then
        sed -i "s|^export ${key}=.*|export ${key}=\"${val}\"|" "$ENV_FILE"
    else
        echo "export ${key}=\"${val}\"" >> "$ENV_FILE"
    fi
    export "${key}=${val}"
    echo -e "${G}✓${N} ${key} updated"
}

cmd_show() {
    load_env
    echo ""
    echo -e "${BOLD}🔑 NasTech API Keys${N}"
    echo "───────────────────────────────────"
    echo -e "  GROQ_API_KEY:       $(mask_val "${GROQ_API_KEY:-}")"
    echo -e "  GEMINI_API_KEY:     $(mask_val "${GEMINI_API_KEY:-}")"
    echo -e "  OPENROUTER_API_KEY: $(mask_val "${OPENROUTER_API_KEY:-}")"
    echo -e "  TELEGRAM_BOT_TOKEN: $(mask_val "${TELEGRAM_BOT_TOKEN:-}")"
    echo -e "  TELEGRAM_CHAT_ID:   $(mask_val "${TELEGRAM_CHAT_ID:-}")"
    echo -e "  GITHUB_TOKEN:       $(mask_val "${GITHUB_TOKEN:-}")"
    echo ""
    echo -e "  Config file: ${ENV_FILE}"
    echo ""
}

cmd_set() {
    local key="$1" val="$2"
    [[ -z "$key" || -z "$val" ]] && {
        echo -e "${R}Usage: bash nastech_config.sh set KEY value${N}"; exit 1; }
    save_key "$key" "$val"
    echo -e "  Run: ${Y}source ${ENV_FILE}${N} to apply in current shell"
}

cmd_test() {
    load_env
    echo ""
    echo -e "${BOLD}🔍 Testing API Keys…${N}"

    # Groq
    if [[ -n "${GROQ_API_KEY:-}" ]]; then
        resp=$(curl -s -o /dev/null -w "%{http_code}" \
            -H "Authorization: Bearer ${GROQ_API_KEY}" \
            https://api.groq.com/openai/v1/models 2>/dev/null)
        [[ "$resp" == "200" ]] \
            && echo -e "  ${G}✅ Groq — OK${N}" \
            || echo -e "  ${R}❌ Groq — HTTP ${resp}${N}"
    else
        echo -e "  ${R}❌ Groq — not set${N}"
    fi

    # Gemini
    if [[ -n "${GEMINI_API_KEY:-}" ]]; then
        resp=$(curl -s -o /dev/null -w "%{http_code}" \
            "https://generativelanguage.googleapis.com/v1beta/models?key=${GEMINI_API_KEY}" 2>/dev/null)
        [[ "$resp" == "200" ]] \
            && echo -e "  ${G}✅ Gemini — OK${N}" \
            || echo -e "  ${R}❌ Gemini — HTTP ${resp}${N}"
    else
        echo -e "  ${R}❌ Gemini — not set${N}"
    fi

    # OpenRouter
    if [[ -n "${OPENROUTER_API_KEY:-}" ]]; then
        resp=$(curl -s -o /dev/null -w "%{http_code}" \
            -H "Authorization: Bearer ${OPENROUTER_API_KEY}" \
            https://openrouter.ai/api/v1/models 2>/dev/null)
        [[ "$resp" == "200" ]] \
            && echo -e "  ${G}✅ OpenRouter — OK${N}" \
            || echo -e "  ${R}❌ OpenRouter — HTTP ${resp}${N}"
    else
        echo -e "  ${R}❌ OpenRouter — not set${N}"
    fi

    # Telegram
    if [[ -n "${TELEGRAM_BOT_TOKEN:-}" ]]; then
        resp=$(curl -s "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/getMe" 2>/dev/null \
            | python3 -c "import sys,json; d=json.load(sys.stdin); print('OK' if d.get('ok') else 'FAIL')" 2>/dev/null)
        [[ "$resp" == "OK" ]] \
            && echo -e "  ${G}✅ Telegram — OK${N}" \
            || echo -e "  ${R}❌ Telegram — FAIL${N}"
    else
        echo -e "  ${R}❌ Telegram Bot Token — not set${N}"
    fi
    echo ""
}

cmd_interactive() {
    load_env
    echo ""
    echo -e "${BOLD}${C}╔══════════════════════════════════════════════╗${N}"
    echo -e "${BOLD}${C}║  NasTech Config — API Key Manager             ║${N}"
    echo -e "${BOLD}${C}╚══════════════════════════════════════════════╝${N}"
    echo ""
    echo "  1) Show all keys"
    echo "  2) Set GROQ_API_KEY"
    echo "  3) Set GEMINI_API_KEY"
    echo "  4) Set OPENROUTER_API_KEY"
    echo "  5) Set TELEGRAM_BOT_TOKEN"
    echo "  6) Set TELEGRAM_CHAT_ID"
    echo "  7) Set GITHUB_TOKEN"
    echo "  8) Test all keys"
    echo "  q) Quit"
    echo ""
    read -rp "  Choice: " choice
    case "$choice" in
        1) cmd_show ;;
        2) read -rp "  GROQ_API_KEY: " v; save_key GROQ_API_KEY "$v" ;;
        3) read -rp "  GEMINI_API_KEY: " v; save_key GEMINI_API_KEY "$v" ;;
        4) read -rp "  OPENROUTER_API_KEY: " v; save_key OPENROUTER_API_KEY "$v" ;;
        5) read -rp "  TELEGRAM_BOT_TOKEN: " v; save_key TELEGRAM_BOT_TOKEN "$v" ;;
        6) read -rp "  TELEGRAM_CHAT_ID: " v; save_key TELEGRAM_CHAT_ID "$v" ;;
        7) read -rp "  GITHUB_TOKEN: " v; save_key GITHUB_TOKEN "$v" ;;
        8) cmd_test ;;
        q|Q) echo "Bye!"; exit 0 ;;
        *) echo -e "${Y}Unknown choice${N}" ;;
    esac
}

# Main
case "${1:-}" in
    show|list)      cmd_show ;;
    set)            cmd_set "${2:-}" "${3:-}" ;;
    test|check)     cmd_test ;;
    *)              cmd_interactive ;;
esac
CONFIGEOF
chmod +x "${BUNDLE_DIR}/nastech_config.sh"
echo -e "  ${G}✓${N} nastech_config.sh"

# ── Make scripts executable ───────────────────────────────────────────
chmod +x "${BUNDLE_DIR}/termux_install.sh" 2>/dev/null || true
chmod +x "${BUNDLE_DIR}/scripts/telegram_bot/run_bot.sh" 2>/dev/null || true

# ── Create the ZIP ────────────────────────────────────────────────────
echo ""
echo -e "${B}[→]${N} Creating ZIP bundle…"
cd /tmp
if command -v zip &>/dev/null; then
    zip -r "${OUTPUT_ZIP}" "${BUNDLE_NAME}/" -x "*.pyc" -x "*/__pycache__/*" -x "*/.DS_Store"
    mv "${OUTPUT_ZIP}" "${ROOT_DIR}/${OUTPUT_ZIP}"
    ZIP_PATH="${ROOT_DIR}/${OUTPUT_ZIP}"
elif command -v python3 &>/dev/null; then
    python3 - << PYEOF
import zipfile, os, pathlib, shutil
bundle = pathlib.Path("${BUNDLE_DIR}")
out    = pathlib.Path("${ROOT_DIR}/${OUTPUT_ZIP}")
with zipfile.ZipFile(str(out), "w", zipfile.ZIP_DEFLATED) as z:
    for f in bundle.rglob("*"):
        if f.is_file() and "__pycache__" not in str(f) and not str(f).endswith(".pyc"):
            arcname = "${BUNDLE_NAME}/" + str(f.relative_to(bundle))
            z.write(str(f), arcname)
print(f"ZIP created: {out}  ({out.stat().st_size // 1024} KB)")
PYEOF
    ZIP_PATH="${ROOT_DIR}/${OUTPUT_ZIP}"
else
    echo -e "${R}✗ Neither zip nor python3 found!${N}"; exit 1
fi

# ── Show result ───────────────────────────────────────────────────────
ZIP_SIZE=$(du -sh "${ZIP_PATH}" 2>/dev/null | cut -f1 || echo "?")
FILE_COUNT=$(unzip -l "${ZIP_PATH}" 2>/dev/null | tail -1 | awk '{print $2}' || echo "?")

echo ""
echo -e "${BOLD}${G}╔══════════════════════════════════════════════════╗${N}"
echo -e "${BOLD}${G}║  ✅ Bundle created!                               ║${N}"
echo -e "${BOLD}${G}╚══════════════════════════════════════════════════╝${N}"
echo ""
echo -e "  📦 File:    ${BOLD}${ZIP_PATH}${N}"
echo -e "  📏 Size:    ${ZIP_SIZE}"
echo -e "  📁 Files:   ${FILE_COUNT}"
echo ""
echo -e "${BOLD}Install on Termux:${N}"
echo ""
echo -e "  ${C}# Transfer the ZIP to your Android device, then in Termux:${N}"
echo -e "  unzip ${OUTPUT_ZIP} -d ~/nastech-guardian"
echo -e "  cd ~/nastech-guardian/${BUNDLE_NAME}"
echo -e "  bash termux_install.sh"
echo ""
echo -e "  ${C}# Or download directly from GitHub:${N}"
echo -e "  pkg install git -y"
echo -e "  git clone https://github.com/nastech-ai/NasGuardian ~/nastech-guardian"
echo -e "  bash ~/nastech-guardian/termux_install.sh"
echo ""

# ── Clean up temp ─────────────────────────────────────────────────────
rm -rf "${BUNDLE_DIR}"
