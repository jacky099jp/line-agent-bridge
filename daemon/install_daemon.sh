#!/usr/bin/env bash
# 安装本机守护为开机自启（macOS / launchd）
#
#   ./install_daemon.sh                      # 用默认配置路径
#   CONFIG=~/x.json PYTHON=/usr/bin/python3 ./install_daemon.sh
#
# 卸载：
#   launchctl bootout gui/$(id -u)/com.example.line-agent-bridge
#   rm ~/Library/LaunchAgents/com.example.line-agent-bridge.plist

set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
LABEL="com.example.line-agent-bridge"
PYTHON="${PYTHON:-$(command -v python3 || true)}"
CONFIG="${CONFIG:-$HOME/.line-agent-bridge/config.json}"
LOG="${LOG:-$HOME/.line-agent-bridge/daemon.log}"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
TEMPLATE="$HERE/com.example.line-agent-bridge.plist.example"

[ -n "$PYTHON" ] && [ -x "$PYTHON" ] || { echo "✗ 找不到 python3（可用 PYTHON=... 指定）"; exit 1; }
[ -f "$CONFIG" ] || { echo "✗ 找不到配置文件 $CONFIG"; echo "  先复制一份改：cp daemon/config.example.json ~/.line-agent-bridge/config.json"; exit 1; }
[ -f "$TEMPLATE" ] || { echo "✗ 找不到模板 $TEMPLATE"; exit 1; }

mkdir -p "$(dirname "$LOG")" "$HOME/Library/LaunchAgents"

sed -e "s|__PYTHON__|$PYTHON|g" \
    -e "s|__SCRIPT__|$HERE/line_daemon.py|g" \
    -e "s|__CONFIG__|$CONFIG|g" \
    -e "s|__LOG__|$LOG|g" \
    "$TEMPLATE" > "$PLIST"

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"

echo "✓ 已安装并启动：$LABEL"
echo "  配置：$CONFIG"
echo "  日志：$LOG"
echo
echo "  看运行状态：launchctl print gui/$(id -u)/$LABEL | head -20"
echo "  看日志：    tail -f \"$LOG\""
echo "  卸载：      launchctl bootout gui/$(id -u)/$LABEL && rm \"$PLIST\""
