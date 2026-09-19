#!/bin/bash
# iperf3-fleet agent 一键安装脚本（由面板下发）
# 用法: curl -fsSL http://面板地址/agent/install.sh | bash -s -- http://面板地址 接入令牌 签名密钥
set -e

PANEL_URL="${1:-}"
TOKEN="${2:-}"
SIGN_KEY="${3:-}"
if [ -z "$PANEL_URL" ] || [ -z "$TOKEN" ] || [ -z "$SIGN_KEY" ]; then
  if [ -n "$PANEL_URL" ] && [ -n "$TOKEN" ] && [ -z "$SIGN_KEY" ]; then
    echo "❌ 检测到旧版接入命令（缺少第 3 个参数「签名密钥」）"
    echo "   请回到面板 → 机器管理 → 「接入命令」，重新复制最新的接入命令后执行。"
  else
    echo "用法: curl -fsSL http://面板地址/agent/install.sh | bash -s -- http://面板地址 接入令牌 签名密钥"
  fi
  exit 1
fi
case "$PANEL_URL" in http://*|https://*) ;; *) PANEL_URL="http://$PANEL_URL" ;; esac

if [ "$(id -u)" != "0" ]; then
  echo "❌ 请使用 root 用户执行（或 sudo bash）"
  exit 1
fi

ensure_pkg() { # $1=命令 $2=包名(apt) $3=包名(alpine)
  command -v "$1" >/dev/null 2>&1 && return 0
  echo "[install] 正在安装 $2 ..."
  apt-get install -y "$2" 2>/dev/null || yum install -y "$2" 2>/dev/null \
    || dnf install -y "$2" 2>/dev/null || apk add --no-cache "$3" 2>/dev/null || return 1
  command -v "$1" >/dev/null 2>&1
}

if ! ensure_pkg curl curl curl; then
  echo "❌ curl 安装失败，请手动安装 curl 后重试"
  exit 1
fi
if ! ensure_pkg openssl openssl openssl; then
  echo "❌ openssl 安装失败（Agent 需要它校验任务签名）"
  exit 1
fi

mkdir -p /usr/local/lib/iperf3-fleet /etc/iperf3-fleet /var/lib/iperf3-fleet

cat > /usr/local/lib/iperf3-fleet/agent.sh << 'AGENT_EOF'
#!/bin/bash
# iperf3-fleet agent 主循环：心跳领取任务 → 验签 → 执行 → 流式回传输出
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

# 任务签名校验：签名密钥只在接入时下发一次，任何没有签名的任务一律拒绝执行
verify_sig() { # $1=job_id $2=cmd_b64 $3=sig
  [ -n "$SIGN_KEY" ] || return 1
  [ -n "$3" ] || return 1
  calc=$(printf '%s' "$1:$2" | openssl dgst -sha256 -hmac "$SIGN_KEY" 2>/dev/null | awk '{print $NF}')
  [ -n "$calc" ] && [ "$calc" = "$3" ]
}

while true; do
  resp=$(heartbeat)
  hb_rc=$?
  if [ $hb_rc -ne 0 ]; then
    echo "$(date '+%F %T') 心跳失败 curl_exit=$hb_rc（7=连接被拒 28=超时 22=令牌被面板拒绝）" >> "$SPOOL/agent.log"
    sleep 5
    continue
  fi
  job_id=$(printf '%s\n' "$resp" | sed -n 's/^job_id=//p')
  if [ -z "$job_id" ]; then
    sleep 3
    continue
  fi
  timeout=$(printf '%s\n' "$resp" | sed -n 's/^timeout=//p')
  timeout=${timeout:-120}
  sig=$(printf '%s\n' "$resp" | sed -n 's/^sig=//p')
  cmd_b64=$(printf '%s\n' "$resp" | sed -n 's/^cmd_b64=//p')

  if ! verify_sig "$job_id" "$cmd_b64" "$sig"; then
    echo "$(date '+%F %T') 拒绝执行：任务 #$job_id 签名校验失败（疑似被篡改）" >> "$SPOOL/agent.log"
    sleep 5
    continue
  fi

  # 防重放：数值任务编号必须递增（墓碑等特殊任务除外）
  case "$job_id" in
    ''|*[!0-9]*) ;;
    *) last=$(cat "$SPOOL/last_job_id" 2>/dev/null || echo 0)
       if [ "$job_id" -le "$last" ]; then
         echo "$(date '+%F %T') 拒绝执行：任务 #$job_id 为重放" >> "$SPOOL/agent.log"
         sleep 5
         continue
       fi
       echo "$job_id" > "$SPOOL/last_job_id" ;;
  esac

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
SIGN_KEY='$SIGN_KEY'
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
  systemctl enable iperf3-fleet-agent >/dev/null 2>&1
  # 必须用 restart：重复安装时 enable --now 不会重启已运行的服务，
  # 会导致新令牌/签名密钥不生效、Agent 一直离线
  systemctl restart iperf3-fleet-agent
  if ! systemctl is-active --quiet iperf3-fleet-agent; then
    echo "❌ systemd 启动 agent 失败，请执行 journalctl -u iperf3-fleet-agent -n 20 查看原因"
    exit 1
  fi
  echo "✅ Agent 已安装并通过 systemd 启动（服务名: iperf3-fleet-agent）"
else
  pkill -f "iperf3-fleet/agent.sh" 2>/dev/null || true
  (setsid nohup bash /usr/local/lib/iperf3-fleet/agent.sh >> /var/lib/iperf3-fleet/agent.log 2>&1 &)
  (crontab -l 2>/dev/null | grep -v "iperf3-fleet/agent.sh"
   echo "@reboot bash /usr/local/lib/iperf3-fleet/agent.sh >> /var/lib/iperf3-fleet/agent.log 2>&1") | crontab - 2>/dev/null || true
  echo "✅ Agent 已安装并通过 nohup+cron 启动（未检测到 systemd）"
fi

echo "[install] 检查本机到面板的连通性..."
if curl -fsS -m 5 "$PANEL_URL/api/health" >/dev/null 2>&1; then
  echo "✅ 面板连通性正常"
else
  echo "⚠️ 警告：本机访问不到面板 $PANEL_URL/api/health"
  echo "   Agent 将无法上线！请在面板机放行该端口（防火墙/云安全组），"
  echo "   然后执行: systemctl restart iperf3-fleet-agent"
  exit 1
fi

echo "正在等待面板确认接入（约 3 秒）..."
sleep 3
echo "完成。回到面板刷新即可看到机器上线。"
