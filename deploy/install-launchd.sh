#!/usr/bin/env bash
# Install the Court Share Bot as a macOS launchd agent.
#
# After install:
#   • The bot starts automatically when you log in.
#   • If it crashes or you kill it, launchd restarts it within 10 seconds.
#   • Logs go to ./logs/bot.out and ./logs/bot.err (next to this repo).
#
# To uninstall:  ./deploy/install-launchd.sh uninstall

set -euo pipefail

LABEL="com.miladmomeni.courtsharebot"
PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PLIST="$HOME/Library/LaunchAgents/${LABEL}.plist"

# Find python3. Prefer the venv at .venv if it exists.
if [ -x "${PROJECT_DIR}/.venv/bin/python3" ]; then
    PYTHON="${PROJECT_DIR}/.venv/bin/python3"
else
    PYTHON="$(command -v python3)"
fi

mkdir -p "${PROJECT_DIR}/logs"

case "${1:-install}" in
  install)
    cat > "${PLIST}" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${LABEL}</string>

    <key>WorkingDirectory</key>
    <string>${PROJECT_DIR}</string>

    <!-- Source .env before running, so TELEGRAM_BOT_TOKEN is in the env. -->
    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>-c</string>
        <string>set -a; [ -f .env ] && source .env; set +a; exec ${PYTHON} fetch_telegram_polls.py</string>
    </array>

    <!-- Start at login -->
    <key>RunAtLoad</key>
    <true/>

    <!-- Restart if it ever exits, but throttle to once every 10s -->
    <key>KeepAlive</key>
    <true/>
    <key>ThrottleInterval</key>
    <integer>10</integer>

    <key>StandardOutPath</key>
    <string>${PROJECT_DIR}/logs/bot.out</string>
    <key>StandardErrorPath</key>
    <string>${PROJECT_DIR}/logs/bot.err</string>

    <key>ProcessType</key>
    <string>Background</string>
</dict>
</plist>
PLIST

    # Reload (unload first in case it was already loaded)
    launchctl unload "${PLIST}" 2>/dev/null || true
    launchctl load "${PLIST}"

    echo "✓ Installed ${LABEL}"
    echo "  Plist: ${PLIST}"
    echo "  Python: ${PYTHON}"
    echo "  Logs:   ${PROJECT_DIR}/logs/bot.{out,err}"
    echo ""
    echo "Useful commands:"
    echo "  tail -f ${PROJECT_DIR}/logs/bot.out      # follow live logs"
    echo "  launchctl list | grep ${LABEL}           # is it running?"
    echo "  launchctl unload ${PLIST}                # stop the bot"
    echo "  launchctl load   ${PLIST}                # start it again"
    echo "  ${0} uninstall                            # remove the agent"
    ;;

  uninstall)
    if [ -f "${PLIST}" ]; then
        launchctl unload "${PLIST}" 2>/dev/null || true
        rm "${PLIST}"
        echo "✓ Uninstalled ${LABEL}"
    else
        echo "Nothing to uninstall — ${PLIST} not found."
    fi
    ;;

  status)
    if launchctl list | grep -q "${LABEL}"; then
        echo "✓ Running:"
        launchctl list | grep "${LABEL}"
    else
        echo "✗ Not running."
    fi
    ;;

  *)
    echo "Usage: $0 [install|uninstall|status]"
    exit 1
    ;;
esac
