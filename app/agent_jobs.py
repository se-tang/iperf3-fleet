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

# 每轮测试开始：清掉旧进程后启动 iperf3 -s
SCRIPT_START_SERVER = r'''
pkill -x iperf3 2>/dev/null
sleep 0.5
(setsid nohup iperf3 -s > /tmp/iperf3-server.log 2>&1 &)
sleep 1
if pgrep -x iperf3 >/dev/null 2>&1; then
  echo SERVER_STARTED
else
  echo SERVER_FAILED
  cat /tmp/iperf3-server.log 2>/dev/null
  exit 1
fi
'''.strip()

# 每台后端测试前：确保目标机 server 存活（掉线自动重启）
SCRIPT_ENSURE_SERVER = r'''
if pgrep -x iperf3 >/dev/null 2>&1; then
  echo ALREADY_RUNNING
else
  (setsid nohup iperf3 -s > /tmp/iperf3-server.log 2>&1 &)
  sleep 1
  if pgrep -x iperf3 >/dev/null 2>&1; then
    echo SERVER_STARTED
  else
    echo SERVER_FAILED
    cat /tmp/iperf3-server.log 2>/dev/null
    exit 1
  fi
fi
'''.strip()

SCRIPT_STOP_SERVER = 'pkill -x iperf3 2>/dev/null; echo IPERF3_SERVER_STOPPED'


def script_check_port(ip):
    """开始测速前先探测目标机 5201 端口（bash /dev/tcp，无需 nc），防火墙未放行时给出明确报错。"""
    return (
        "if timeout 4 bash -c 'exec 3<>/dev/tcp/{ip}/5201' 2>/dev/null; then\n"
        "  echo PORT_5201_OK\n"
        "else\n"
        "  echo PORT_5201_BLOCKED\n"
        "  exit 1\n"
        "fi"
    ).format(ip=ip)
