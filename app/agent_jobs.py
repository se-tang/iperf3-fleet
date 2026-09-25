"""派发给 Agent 执行的 shell 任务脚本（在目标机器上运行，要求 root）。"""
import ipaddress
import re


def valid_port(port, default=5201):
    """把用户填的端口收敛成 1–65535 的整数，非法值退回默认端口。"""
    try:
        p = int(str(port).strip())
    except (TypeError, ValueError):
        return default
    return p if 1 <= p <= 65535 else default


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
# 端口由用户自定义（默认 5201），模板里用 __PORT__ 占位后替换，避免 format 转义问题
SCRIPT_START_SERVER_TMPL = r'''
[ -f /tmp/iperf3-server.pid ] && kill "$(cat /tmp/iperf3-server.pid)" 2>/dev/null
command -v pkill >/dev/null 2>&1 && pkill -f 'iperf3 -s' 2>/dev/null
sleep 0.5
nohup timeout 1800 iperf3 -s -p __PORT__ > /tmp/iperf3-server.log 2>&1 &
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


def script_start_server(port=5201):
    """启动目标机的 iperf3 server（TCP/UDP 共用同一个 -s 进程，端口可自定义）。"""
    return SCRIPT_START_SERVER_TMPL.replace('__PORT__', str(valid_port(port)))


SCRIPT_STOP_SERVER = r'''
[ -f /tmp/iperf3-server.pid ] && kill "$(cat /tmp/iperf3-server.pid)" 2>/dev/null
command -v pkill >/dev/null 2>&1 && pkill -f 'iperf3 -s' 2>/dev/null
echo IPERF3_SERVER_STOPPED
'''.strip()


def script_check_port(ip, port=5201):
    """探测目标机 iperf3 端口是否可达。IPv4 优先用 bash /dev/tcp（瞬时完成），不可用时回退 curl；
    IPv6 直接用 curl（bash 的 /dev/tcp 不支持 IPv6 字面量，地址需带方括号）。
    连接被拒=7 / 连接超时=28 视为不通，其余视为可达（iperf3 不是 HTTP，能连上就算通）。"""
    port = valid_port(port)
    host = f'[{ip}]' if ipaddress.ip_address(ip).version == 6 else ip
    return (
        'host="{host}"; port="{port}"\n'
        'probe() {{\n'
        '  if [ {ipv6} != "1" ] && command -v bash >/dev/null 2>&1; then\n'
        '    if timeout 4 bash -c "exec 3<>/dev/tcp/$host/$port" 2>/dev/null; then return 0; fi\n'
        '  fi\n'
        '  if command -v curl >/dev/null 2>&1; then\n'
        '    curl -s -o /dev/null --connect-timeout 3 -m 4 "http://$host:$port/" >/dev/null 2>&1\n'
        '    rc=$?\n'
        '    [ "$rc" != "7" ] && [ "$rc" != "28" ] && return 0\n'
        '  fi\n'
        '  return 1\n'
        '}}\n'
        'if probe; then echo PORT_OK; else echo PORT_BLOCKED; exit 1; fi'
    ).format(host=host, port=port, ipv6='1' if ipaddress.ip_address(ip).version == 6 else '0')


# 探测本机可用于测试的全局 IPv4 / IPv6 地址。
# 只做本地查询（ip route get 是路由查表，不发送任何数据包），不访问第三方网站；
# 输出 IPV4= / IPV6=（缺失则留空），面板解析后写入机器资料。
# 用途：面板只监听 IPv4 时也能拿到机器的 IPv6 地址，避免必须支持双栈面板。
SCRIPT_REPORT_IPS = r'''
r4=""
r6=""
if command -v ip >/dev/null 2>&1; then
  r4=$(ip -4 route get 1.1.1.1 2>/dev/null | sed -n 's/.* src \([0-9][0-9.]*\).*/\1/p' | head -n1)
  # 全局 IPv6：排除已弃用地址、ULA(fc/fd) 与链路本地(fe80)。
  # 优先取稳定地址（开启隐私扩展的机器会额外生成 temporary 地址）；
  # 若该机只有临时地址，退而使用临时地址——否则这类机器会完全探测不到 IPv6。
  cands=$(ip -6 addr show scope global 2>/dev/null | awk '
    /inet6/ && $0 !~ /deprecated/ {
      a = $2; sub(/\/.*/, "", a);
      if (a ~ /^(f[cd]|fe80)/) next;
      print ($0 ~ /temporary/ ? "tmp" : "stable"), a;
    }')
  r6=$(printf '%s\n' "$cands" | awk '$1 == "stable" {print $2; exit}')
  [ -z "$r6" ] && r6=$(printf '%s\n' "$cands" | awk 'NF {print $2; exit}')
fi
[ -z "$r4" ] && r4=$(hostname -I 2>/dev/null | tr ' ' '\n' | grep -E '^[0-9]{1,3}(\.[0-9]{1,3}){3}$' | head -n1)
[ -z "$r6" ] && r6=$(hostname -I 2>/dev/null | tr ' ' '\n' | grep ':' | grep -v -iE '^(fe80|f[cd])' | head -n1)
echo "IPV4=${r4}"
echo "IPV6=${r6}"
'''.strip()


def parse_ip_report(out):
    """解析 SCRIPT_REPORT_IPS 的输出，只接受「协议族与字段匹配 + 全局可路由」的地址。
    返回如 {'ip4': '1.2.3.4'} / {'ip6': '2408:...'}，无有效地址时为空字典。"""
    found = {}
    for line in (out or '').splitlines():
        m = re.match(r'^(IPV4|IPV6)=(.*)$', line.strip())
        if not m:
            continue
        key = 'ip4' if m.group(1) == 'IPV4' else 'ip6'
        try:
            addr = ipaddress.ip_address(m.group(2).strip())
        except ValueError:
            continue
        if addr.version != (4 if key == 'ip4' else 6) or not addr.is_global:
            continue
        found[key] = addr.compressed
    return found
