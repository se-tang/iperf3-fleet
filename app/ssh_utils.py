"""SSH 远程执行工具：连接机器、执行命令流式读日志、确保 iperf3 可用。"""
import io
import time

import paramiko


class RunAborted(Exception):
    """用户请求停止测试。"""


# 在远端机器上检查并安装 iperf3 / ping（apt → dnf → yum+epel → apk → zypper 依次尝试）
SCRIPT_ENSURE = r'''
export DEBIAN_FRONTEND=noninteractive
SUDO=""
if [ "$(id -u)" != "0" ] && command -v sudo >/dev/null 2>&1; then SUDO="sudo -n"; fi
$SUDO true 2>/dev/null || SUDO=""
have() { command -v "$1" >/dev/null 2>&1; }
if ! have ping; then
  echo "[setup] ping 未找到，尝试安装 iputils..."
  $SUDO apt-get install -y iputils-ping 2>/dev/null \
    || $SUDO yum install -y iputils 2>/dev/null \
    || $SUDO dnf install -y iputils 2>/dev/null \
    || $SUDO apk add --no-cache iputils-ping 2>/dev/null \
    || echo "[setup] ping 安装失败（可能影响 ping 测试）"
fi
if have iperf3; then
  echo "[setup] iperf3 已安装: $(iperf3 --version 2>&1 | head -n1)"
  exit 0
fi
echo "[setup] 未检测到 iperf3，开始自动安装..."
ok=0
if have apt-get; then
  $SUDO apt-get update -y >/dev/null 2>&1
  $SUDO apt-get install -y iperf3 >/dev/null 2>&1 && ok=1
fi
if [ "$ok" = "0" ] && have dnf; then
  $SUDO dnf install -y iperf3 >/dev/null 2>&1 && ok=1
fi
if [ "$ok" = "0" ] && have yum; then
  $SUDO yum install -y epel-release >/dev/null 2>&1
  $SUDO yum install -y iperf3 >/dev/null 2>&1 && ok=1
fi
if [ "$ok" = "0" ] && have apk; then
  $SUDO apk add --no-cache iperf3 >/dev/null 2>&1 && ok=1
fi
if [ "$ok" = "0" ] && have zypper; then
  $SUDO zypper --non-interactive install iperf3 >/dev/null 2>&1 && ok=1
fi
if have iperf3; then
  echo "[setup] iperf3 安装成功: $(iperf3 --version 2>&1 | head -n1)"
else
  echo "[setup] iperf3 自动安装失败，请手动安装后重试"
  exit 1
fi
'''.strip()

# 确保目标机上 iperf3 -s 在运行（没有则启动，掉线则重启）
SCRIPT_ENSURE_SERVER = r'''
if pgrep -x iperf3 >/dev/null 2>&1; then
  echo ALREADY_RUNNING
else
  if command -v setsid >/dev/null 2>&1; then
    (setsid nohup iperf3 -s > /tmp/iperf3-server.log 2>&1 &)
  else
    (nohup iperf3 -s > /tmp/iperf3-server.log 2>&1 &)
  fi
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


class LineBuffer:
    """把任意分块的数据按行切开后回调。"""

    def __init__(self, cb):
        self.cb = cb
        self.buf = ''

    def feed(self, text):
        self.buf += text
        while '\n' in self.buf:
            line, self.buf = self.buf.split('\n', 1)
            self.cb(line.rstrip('\r'))

    def flush(self):
        if self.buf:
            self.cb(self.buf)
            self.buf = ''


def load_pkey(key_str, passphrase=None):
    last_err = None
    classes = [paramiko.RSAKey, paramiko.Ed25519Key, paramiko.ECDSAKey]
    if hasattr(paramiko, 'DSSKey'):
        classes.append(paramiko.DSSKey)
    for cls in classes:
        try:
            return cls.from_private_key(io.StringIO(key_str), password=passphrase or None)
        except paramiko.PasswordRequiredException:
            raise ValueError('私钥需要口令(passphrase)，请在表单中填写')
        except Exception as e:
            last_err = e
    raise ValueError(f'无法解析私钥: {last_err}')


def connect(m):
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    kwargs = dict(
        hostname=m['host'], port=int(m['ssh_port'] or 22),
        username=m['ssh_user'] or 'root',
        timeout=15, banner_timeout=20, auth_timeout=20,
        allow_agent=False, look_for_keys=False)
    if (m.get('auth_type') or 'password') == 'key':
        kwargs['pkey'] = load_pkey(m.get('private_key') or '', m.get('key_passphrase'))
    else:
        kwargs['password'] = m.get('password') or ''
    ssh.connect(**kwargs)
    transport = ssh.get_transport()
    if transport:
        transport.set_keepalive(15)
    return ssh


def run_cmd(ssh, cmd, timeout=60, logcb=None, stop_check=None):
    """执行命令，实时回传输出，返回 (exit_code, 完整输出)。"""
    transport = ssh.get_transport()
    if transport is None or not transport.is_active():
        raise RuntimeError('SSH 连接已断开')
    chan = transport.open_session(timeout=15)
    chan.settimeout(5.0)
    chan.exec_command(cmd)
    chunks = []
    lb = LineBuffer(logcb) if logcb else None
    start = time.time()
    while True:
        if stop_check is not None:
            try:
                stop_check()
            except BaseException:
                chan.close()
                raise
        got = False
        while chan.recv_ready():
            data = chan.recv(8192).decode('utf-8', 'replace')
            chunks.append(data)
            if lb:
                lb.feed(data)
            got = True
        while chan.recv_stderr_ready():
            data = chan.recv_stderr(8192).decode('utf-8', 'replace')
            chunks.append(data)
            if lb:
                lb.feed(data)
            got = True
        if chan.exit_status_ready():
            while chan.recv_ready():
                data = chan.recv(8192).decode('utf-8', 'replace')
                chunks.append(data)
                if lb:
                    lb.feed(data)
                got = True
            while chan.recv_stderr_ready():
                data = chan.recv_stderr(8192).decode('utf-8', 'replace')
                chunks.append(data)
                if lb:
                    lb.feed(data)
                got = True
            status = chan.recv_exit_status()
            break
        if time.time() - start > timeout:
            chan.close()
            raise TimeoutError(f'命令执行超时({timeout}s): {cmd[:60]}')
        if not got:
            time.sleep(0.15)
    if lb:
        lb.flush()
    return status, ''.join(chunks)


def ensure_server(ssh, logcb=None):
    """确保目标机 iperf3 -s 正在运行。"""
    code, out = run_cmd(ssh, SCRIPT_ENSURE_SERVER, timeout=30, logcb=logcb)
    if code != 0:
        raise RuntimeError('iperf3 server 启动失败: ' + (out or '').strip()[-300:])
    return (out or '').strip()


def stop_server(ssh):
    try:
        run_cmd(ssh, SCRIPT_STOP_SERVER, timeout=15)
    except Exception:
        pass
