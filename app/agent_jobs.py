"""派发给 Agent 执行的 shell 任务脚本（在目标机器上运行，要求 root）。"""

# 检查并安装 iperf3 / ping（apt → dnf → yum+epel → apk → zypper 依次尝试）
SCRIPT_ENSURE = r'''
export DEBIAN_FRONTEND=noninteractive
have() { command -v "$1" >/dev/null 2>&1; }
if ! have ping; then
  echo "[setup] ping 未找到，尝试安装 iputils..."
  apt-get install -y iputils-ping 2>/dev/null \
    || yum install -y iputils 2>/dev/null \
    || dnf install -y iputils 2>/dev/null \
    || apk add --no-cache iputils-ping 2>/dev/null \
    || echo "[setup] ping 安装失败（可能影响 ping 测试）"
fi
if have iperf3; then
  echo "[setup] iperf3 已安装: $(iperf3 --version 2>&1 | head -n1)"
  exit 0
fi
echo "[setup] 未检测到 iperf3，开始自动安装..."
ok=0
if have apt-get; then
  apt-get update -y >/dev/null 2>&1
  apt-get install -y iperf3 >/dev/null 2>&1 && ok=1
fi
if [ "$ok" = "0" ] && have dnf; then
  dnf install -y iperf3 >/dev/null 2>&1 && ok=1
fi
if [ "$ok" = "0" ] && have yum; then
  yum install -y epel-release >/dev/null 2>&1
  yum install -y iperf3 >/dev/null 2>&1 && ok=1
fi
if [ "$ok" = "0" ] && have apk; then
  apk add --no-cache iperf3 >/dev/null 2>&1 && ok=1
fi
if [ "$ok" = "0" ] && have zypper; then
  zypper --non-interactive install iperf3 >/dev/null 2>&1 && ok=1
fi
if have iperf3; then
  echo "[setup] iperf3 安装成功: $(iperf3 --version 2>&1 | head -n1)"
else
  echo "[setup] iperf3 自动安装失败，请手动安装后重试"
  exit 1
fi
'''.strip()

# 每轮测试开始：清掉旧进程后启动 iperf3 -s（也用于通道内掉线自愈重启）
# 用 PID 文件管理进程，不硬依赖 pgrep/pkill（后者仅作为残留进程的补充清扫）
# timeout 1800：server 30 分钟自过期，防止面板崩溃/任务中断后残留进程被扫描器滥用
# （过期后若有后续测试，通道内端口预检失败会自动重启 server）
SCRIPT_START_SERVER = r'''
[ -f /tmp/iperf3-server.pid ] && kill "$(cat /tmp/iperf3-server.pid)" 2>/dev/null
command -v pkill >/dev/null 2>&1 && pkill -f 'iperf3 -s' 2>/dev/null
sleep 0.5
nohup timeout 1800 iperf3 -s > /tmp/iperf3-server.log 2>&1 &
echo $! > /tmp/iperf3-server.pid
sleep 1
if kill -0 "$(cat /tmp/iperf3-server.pid)" 2>/dev/null; then
  echo SERVER_STARTED
else
  echo SERVER_FAILED
  cat /tmp/iperf3-server.log 2>/dev/null
  exit 1
fi
'''.strip()

SCRIPT_STOP_SERVER = r'''
[ -f /tmp/iperf3-server.pid ] && kill "$(cat /tmp/iperf3-server.pid)" 2>/dev/null
command -v pkill >/dev/null 2>&1 && pkill -f 'iperf3 -s' 2>/dev/null
echo IPERF3_SERVER_STOPPED
'''.strip()


def script_check_port(ip):
    """探测目标机 5201 端口：优先 bash /dev/tcp（瞬时完成），不可用时回退 curl
    （连接被拒=7 / 连接超时=28 视为不通，其余视为可达）。"""
    return (
        'target="{ip}"; port="5201"\n'
        'probe() {{\n'
        '  if timeout 4 bash -c "exec 3<>/dev/tcp/$target/$port" 2>/dev/null; then return 0; fi\n'
        '  if command -v curl >/dev/null 2>&1; then\n'
        '    curl -s -o /dev/null --connect-timeout 3 -m 4 "http://$target:$port/" >/dev/null 2>&1\n'
        '    rc=$?\n'
        '    [ "$rc" != "7" ] && [ "$rc" != "28" ] && return 0\n'
        '  fi\n'
        '  return 1\n'
        '}}\n'
        'if probe; then echo PORT_OK; else echo PORT_BLOCKED; exit 1; fi'
    ).format(ip=ip)
