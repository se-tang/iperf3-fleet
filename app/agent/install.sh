#!/bin/bash
# iperf3-fleet agent 一键安装脚本（由面板下发）
# 用法: curl -fsSL http://面板地址/agent/install.sh | bash -s -- http://面板地址 接入令牌
set -e

PANEL_URL="${1:-}"
TOKEN="${2:-}"
if [ -z "$PANEL_URL" ] || [ -z "$TOKEN" ]; then
  echo "用法: curl -fsSL http://面板地址/agent/install.sh | bash -s -- http://面板地址 接入令牌"
  exit 1
fi
case "$PANEL_URL" in http://*|https://*) ;; *) PANEL_URL="http://$PANEL_URL" ;; esac

if [ "$(id -u)" != "0" ]; then
  echo "❌ 请使用 root 用户执行（或 sudo bash）"
  exit 1
fi

if ! command -v curl >/dev/null 2>&1; then
  echo "[install] 正在安装 curl ..."
  apt-get install -y curl 2>/dev/null || yum install -y curl 2>/dev/null \
    || dnf install -y curl 2>/dev/null || apk add --no-cache curl 2>/dev/null || {
      echo "❌ curl 安装失败，请手动安装 curl 后重试"; exit 1; }
fi

mkdir -p /usr/local/lib/iperf3-fleet /etc/iperf3-fleet /var/lib/iperf3-fleet

cat > /usr/local/lib/iperf3-fleet/agent.sh << 'AGENT_EOF'
#!/bin/bash
# iperf3-fleet agent 主循环：心跳领取任务 → 执行 → 流式回传输出
CONF=/etc/iperf3-fleet/agent.conf
[ -f "$CONF" ] || exit 1
. "$CONF"
SPOOL=/var/lib/iperf3-fleet
mkdir -p "$SPOOL" 2>/dev/null

heartbeat() {
  curl -fsS -m 20 -X POST "$PANEL_URL/api/agent/heartbeat" \
    -H "X-Agent-Token: $TOKEN" -H "X-Agent-Host: $(hostname)" 2>/dev/null
}

send_output() { # $1=job_id $2=file $3=done $4=exit_code
  curl -fsS -m 20 -X POST "$PANEL_URL/api/agent/output" \
    -H "X-Agent-Token: $TOKEN" \
    --data-urlencode "job_id=$1" \
    --data-urlencode "text@$2" \
    --data-urlencode "done=$3" \
    --data-urlencode "exit_code=$4" >/dev/null 2>&1
}

while true; do
  resp=$(heartbeat) || { sleep 5; continue; }
  job_id=$(printf '%s\n' "$resp" | sed -n 's/^job_id=//p')
  if [ -z "$job_id" ]; then
    sleep 3
    continue
  fi
  timeout=$(printf '%s\n' "$resp" | sed -n 's/^timeout=//p')
  timeout=${timeout:-120}
  printf '%s\n' "$resp" | sed -n 's/^cmd_b64=//p' | base64 -d > "$SPOOL/job.sh" 2>/dev/null

  out="$SPOOL/job.out"
  : > "$out"
  bash "$SPOOL/job.sh" > "$out" 2>&1 &
  pid=$!
  offset=0
  start=$(date +%s)

  while kill -0 "$pid" 2>/dev/null; do
    size=$(wc -c < "$out" 2>/dev/null || echo 0)
    if [ "$size" -gt "$offset" ]; then
      tail -c +"$((offset + 1))" "$out" > "$SPOOL/chunk" 2>/dev/null
      offset=$size
      send_output "$job_id" "$SPOOL/chunk" 0 ""
    fi
    now=$(date +%s)
    if [ $((now - start)) -gt $((timeout + 120)) ]; then
      kill "$pid" 2>/dev/null
    fi
    sleep 1
  done

  wait "$pid"
  exit_code=$?
  size=$(wc -c < "$out" 2>/dev/null || echo 0)
  if [ "$size" -gt "$offset" ]; then
    tail -c +"$((offset + 1))" "$out" > "$SPOOL/chunk" 2>/dev/null
    send_output "$job_id" "$SPOOL/chunk" 0 ""
  fi
  : > "$SPOOL/done"
  send_output "$job_id" "$SPOOL/done" 1 "$exit_code"
done
AGENT_EOF
chmod 700 /usr/local/lib/iperf3-fleet/agent.sh

cat > /etc/iperf3-fleet/agent.conf << CONF_EOF
PANEL_URL='$PANEL_URL'
TOKEN='$TOKEN'
CONF_EOF
chmod 600 /etc/iperf3-fleet/agent.conf

if [ -d /run/systemd/system ] && command -v systemctl >/dev/null 2>&1; then
  cat > /etc/systemd/system/iperf3-fleet-agent.service << 'UNIT_EOF'
[Unit]
Description=iperf3-fleet agent
After=network-online.target
Wants=network-online.target

[Service]
ExecStart=/bin/bash /usr/local/lib/iperf3-fleet/agent.sh
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT_EOF
  systemctl daemon-reload
  systemctl enable --now iperf3-fleet-agent >/dev/null 2>&1 || systemctl restart iperf3-fleet-agent
  echo "✅ Agent 已安装并通过 systemd 启动（服务名: iperf3-fleet-agent）"
else
  pkill -f "iperf3-fleet/agent.sh" 2>/dev/null || true
  (setsid nohup bash /usr/local/lib/iperf3-fleet/agent.sh >> /var/lib/iperf3-fleet/agent.log 2>&1 &)
  (crontab -l 2>/dev/null | grep -v "iperf3-fleet/agent.sh"
   echo "@reboot bash /usr/local/lib/iperf3-fleet/agent.sh >> /var/lib/iperf3-fleet/agent.log 2>&1") | crontab - 2>/dev/null || true
  echo "✅ Agent 已安装并通过 nohup+cron 启动（未检测到 systemd）"
fi

echo "正在等待面板确认接入（约 3 秒）..."
sleep 3
echo "完成。回到面板刷新即可看到机器上线。"
