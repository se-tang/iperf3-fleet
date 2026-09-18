#!/bin/bash
# iperf3-fleet agent 一键卸载脚本（由面板下发，也可单独使用）
systemctl disable --now iperf3-fleet-agent 2>/dev/null
rm -f /etc/systemd/system/iperf3-fleet-agent.service
systemctl daemon-reload 2>/dev/null
pkill -f "iperf3-fleet/agent.sh" 2>/dev/null
crontab -l 2>/dev/null | grep -v "iperf3-fleet/agent.sh" | crontab - 2>/dev/null
rm -rf /usr/local/lib/iperf3-fleet /etc/iperf3-fleet /var/lib/iperf3-fleet
echo "✅ iperf3-fleet agent 已卸载（服务、自启、文件均已清理）"
