#!/usr/bin/env bash
# Install the sasi-mcp nightly refresh launchd job under the user's LaunchAgents.
# Idempotent: safe to re-run after editing the template.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
TEMPLATE="$REPO_ROOT/launchd/com.sasi.faq-refresh.plist.template"
TARGET="$HOME/Library/LaunchAgents/com.sasi.faq-refresh.plist"
LOG_DIR="$HOME/.sasi-mcp/logs"
PYTHON_BIN="${PYTHON_BIN:-$(command -v python3)}"

if [[ ! -f "$TEMPLATE" ]]; then
    echo "template not found: $TEMPLATE" >&2
    exit 1
fi
if [[ -z "$PYTHON_BIN" ]]; then
    echo "python3 not on PATH; set PYTHON_BIN=/path/to/python3 and retry" >&2
    exit 1
fi

mkdir -p "$LOG_DIR" "$(dirname "$TARGET")"

sed \
    -e "s|__PYTHON__|$PYTHON_BIN|g" \
    -e "s|__REPO_ROOT__|$REPO_ROOT|g" \
    -e "s|__LOG_DIR__|$LOG_DIR|g" \
    "$TEMPLATE" > "$TARGET"

# Re-load to pick up changes if previously installed.
launchctl bootout "gui/$UID/com.sasi.faq-refresh" 2>/dev/null || true
launchctl bootstrap "gui/$UID" "$TARGET"

echo "installed: $TARGET"
echo "next run: $(launchctl list | grep com.sasi.faq-refresh || echo '(not yet listed)')"
