"""测试编排：通过 Agent 任务队列远程执行（面板不再直连机器）。

流水线：iperf3 上/下行全局串行（多台同时测速会互相抢带宽，结果作废），
ping 不占测速通道，与后续机器的 iperf3 并行执行，充分利用等待时间。

测试参数每次可自定义（见 normalize_params / build_iperf_cmd）：
线程数 -P、单向时长 -t、iperf3 端口 -p、TCP/UDP（-u -b）、ping 次数 -c。

协议：默认 IPv4（两端都用 IPv4 地址，避免机器缺 IPv6 时失败）；
用户确认双端都支持 IPv6 时可选择 IPv6 测试。地址取自机器资料
（心跳观测 + 面板探测，见 db.machine_test_ip）。
"""
import ipaddress
import json
import re
import shlex
import threading
import time
import traceback

from . import agent_jobs as aj
from . import db, quality

# 测试参数默认值与上限（前端只做提示，这里才是准入门槛）
DEFAULT_PARAMS = {'streams': 1, 'duration': 10, 'port': 5201,
                  'udp': False, 'udp_bandwidth': '100M', 'ping_count': 200}
# UDP 目标带宽：必须带单位（-b 100 在 iperf3 里是 100 bit/s，太容易填错）
_UDP_BW_RE = re.compile(r'^\d{1,5}(?:\.\d{1,3})?[KMG]$')

_state_lock = threading.Lock()
_active = {}  # run_id -> {'stop': bool, 'log': [..]}


def _int_in(raw, lo, hi, label, default=None):
    """把值收敛成 [lo, hi] 内的整数；空值返回 default，非法值报错。"""
    if raw is None or str(raw).strip() == '':
        return default
    try:
        v = int(str(raw).strip())
    except (TypeError, ValueError):
        raise RuntimeError(f'{label}必须是整数（收到 {raw!r}）')
    if not lo <= v <= hi:
        raise RuntimeError(f'{label}需在 {lo}–{hi} 之间（收到 {v}）')
    return v


def normalize_params(data):
    """校验并收敛前端传来的测试参数；非法值直接报错，不静默改动用户输入。"""
    data = data or {}

    def num(key, lo, hi, label):
        return _int_in(data.get(key), lo, hi, label, DEFAULT_PARAMS[key])

    bw = str(data.get('udp_bandwidth') or DEFAULT_PARAMS['udp_bandwidth']).strip().upper()
    if not _UDP_BW_RE.match(bw):
        raise RuntimeError('UDP 目标带宽必须带单位（示例：100M、1G、500K）')
    # 目标机监听端口：留空/0 = 跟随默认端口（目标机在 NAT 后时可以单独指定内网监听端口）
    tp_raw = data.get('target_port')
    target_port = 0 if tp_raw is None or str(tp_raw).strip() in ('', '0') \
        else _int_in(tp_raw, 1, 65535, '目标机监听端口')
    # 每台后端机可以单独指定端口（{机器ID: 端口}），没指定的用默认端口
    ports = {}
    raw_ports = data.get('ports')
    if isinstance(raw_ports, dict):
        for k, v in raw_ports.items():
            try:
                mid = int(str(k).strip())
            except (TypeError, ValueError):
                raise RuntimeError(f'机器编号必须是整数（收到 {k!r}）')
            p = _int_in(v, 1, 65535, f'机器 #{mid} 的连接端口')
            if p is not None:
                ports[mid] = p
    return {
        'streams': num('streams', 1, 32, '线程数'),
        'duration': num('duration', 1, 300, '单向测试时长（秒）'),
        'port': num('port', 1, 65535, 'iperf3 端口'),
        'target_port': target_port,
        'ping_count': num('ping_count', 1, 2000, 'ping 次数'),
        'udp': bool(data.get('udp')),
        'udp_bandwidth': bw,
        'ports': ports,
    }


def run_cfg(run):
    """从测试记录里取出参数（旧记录缺列时退回默认值）。"""
    bw = str((run or {}).get('udp_bandwidth') or DEFAULT_PARAMS['udp_bandwidth']).strip().upper()
    return {
        'streams': int(run.get('streams') or DEFAULT_PARAMS['streams']),
        'duration': int(run.get('duration') or DEFAULT_PARAMS['duration']),
        'port': aj.valid_port(run.get('port') or DEFAULT_PARAMS['port']),
        'target_port': int(run.get('target_port') or 0),
        'ping_count': int(run.get('ping_count') or DEFAULT_PARAMS['ping_count']),
        'udp': bool(int(run.get('udp') or 0)),
        'udp_bandwidth': bw if _UDP_BW_RE.match(bw) else DEFAULT_PARAMS['udp_bandwidth'],
    }


def target_port_of(cfg):
    """目标机 iperf3 -s 实际监听的端口（没单独指定时跟随默认端口）。"""
    return aj.valid_port(cfg.get('target_port') or cfg['port'], cfg['port'])


def item_port(it, cfg):
    """该后端机本次连接目标机的端口（逐台存了就用存的；0/缺失 = 默认端口）。"""
    return aj.valid_port((it or {}).get('port'), cfg['port'])


def item_cfg(cfg, it):
    """把默认参数换成该后端机的参数（端口逐台不同）。"""
    c = dict(cfg)
    c['port'] = item_port(it, cfg)
    return c


def serve_ports(items, cfg):
    """目标机上需要监听的端口集合。

    目标机监听端口必须起；此外每台后端机**单独指定过**的连接端口也一起起
    （多机各用各端口的场景）。只有跟随默认端口的机器不再额外起 server——
    目标机在 NAT 后、外网映射到别的端口时，本机不需要（也可能不能）监听外网端口。
    """
    ports = {target_port_of(cfg)}
    for it in items:
        p = int((it or {}).get('port') or 0)
        if p and p != cfg['port']:
            ports.add(aj.valid_port(p, cfg['port']))
    return sorted(ports)


def build_iperf_cmd(host, cfg, reverse=False):
    """拼 iperf3 客户端命令（host 由调用方保证是合法 IP，仍然 quote 防注入）。

    -P 线程数、-t 时长、-p 端口可自定义；UDP 追加 -u -b（iperf3 的 -b 是**每流**限速，
    所以 -P 4 -b 100M 等于总目标 400M）；reverse=True 即下行（-R）。
    """
    parts = ['iperf3', '-c', shlex.quote(str(host)),
             '-p', str(cfg['port']), '-t', str(cfg['duration']), '-P', str(cfg['streams'])]
    if cfg['udp']:
        parts += ['-u', '-b', cfg['udp_bandwidth']]
    if reverse:
        parts.append('-R')
    return ' '.join(parts)


class RunAborted(Exception):
    """用户请求停止测试。"""


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


class Iperf3Lane:
    """iperf3 测试串行通道：按机器顺序轮流占用，ping 阶段不占通道。"""

    def __init__(self):
        self.cond = threading.Condition()
        self.turn = 0
        self.stopped = False

    def acquire(self, idx):
        with self.cond:
            while self.turn != idx and not self.stopped:
                self.cond.wait(0.5)
            return not self.stopped

    def release(self):
        with self.cond:
            self.turn += 1
            self.cond.notify_all()

    def stop(self):
        with self.cond:
            self.stopped = True
            self.cond.notify_all()


def active_run_id():
    with _state_lock:
        for rid in _active:
            return rid
    return None


def stop_run(run_id):
    st = _active.get(run_id)
    if st:
        st['stop'] = True
        return True
    return False


def get_log(run_id):
    st = _active.get(run_id)
    if st:
        return list(st['log'])
    run = db.get_run(run_id)
    return run['log'].splitlines() if run and run['log'] else []


def proto_name(ip_version):
    return 'IPv6' if int(ip_version or 4) == 6 else 'IPv4'


def start_run(target_id, backend_ids, ip_version=4, params=None):
    cfg = normalize_params(params)
    ip_version = 6 if int(ip_version or 4) == 6 else 4
    proto = proto_name(ip_version)
    with _state_lock:
        if _active:
            raise RuntimeError('已有测试任务正在运行，请等待完成或先停止')
        target = db.get_machine(target_id)
        if not target:
            raise RuntimeError('目标机器不存在')
        if target['role'] != 'target':
            raise RuntimeError(f'机器「{target["name"]}」的角色不是「目标机器」，请先在机器管理中修改角色')
        if not backend_ids:
            raise RuntimeError('请至少选择一台后端机器')
        if not db.machine_online(target):
            raise RuntimeError('目标机器的 Agent 未上线，请先在该机器上执行接入命令并等待其上线')
        # 测试地址：默认 IPv4；勾选 IPv6 后使用目标机的 IPv6 地址
        target_ip = db.machine_test_ip(target, ip_version)
        if not target_ip:
            raise RuntimeError(
                f'目标机器「{target["name"]}」还没有 {proto} 地址，无法进行 {proto} 测试'
                f'（可在机器管理里点「探测地址」重试，或确认该机器有 {proto} 出口）')
        for bid in backend_ids:
            m = db.get_machine(bid)
            if not m:
                raise RuntimeError(f'后端机器 #{bid} 不存在')
            if m['role'] != 'backend':
                raise RuntimeError(f'机器「{m["name"]}」的角色不是「后端机器」，不能作为后端参加测试')
        run_id = db.create_run(target, backend_ids, ip_version, target_host=target_ip,
                               **cfg)
        _active[run_id] = {'stop': False, 'log': []}
    threading.Thread(target=_worker, args=(run_id, target, ip_version), daemon=True).start()
    return run_id


def _job(machine, cmd, timeout, log, stop_check):
    """向机器派发一个任务并等待完成，流式转发输出，返回 (exit_code, 输出)。"""
    name = machine['name']
    job_id = db.create_job(machine['id'], cmd, timeout)
    first_line = cmd.strip().splitlines()[0] if cmd.strip() else ''
    log(f'[{name}] 派发任务 #{job_id}: {first_line[:80]}')
    lb = LineBuffer(lambda line: log(f'[{name}] {line}'))
    offset = 0
    start = time.time()
    while True:
        if stop_check is not None:
            stop_check()
        job = db.get_job(job_id)
        out = job['output'] or ''
        if len(out) > offset:
            lb.feed(out[offset:])
            offset = len(out)
        if job['status'] in ('finished', 'failed'):
            lb.flush()
            code = job['exit_code'] if job['exit_code'] is not None else -1
            return code, out
        if job['status'] == 'queued' and time.time() - start > 45:
            db.fail_job(job_id, 'agent 未领取任务（可能离线）')
            raise RuntimeError(f'[{name}] agent 未领取任务（可能离线），请确认机器在线')
        if time.time() - start > timeout + 90:
            db.fail_job(job_id, '任务执行超时')
            raise TimeoutError(f'[{name}] 任务执行超时({timeout}s): {first_line[:60]}')
        time.sleep(0.8)


def _worker(run_id, target, ip_version=4):
    st = _active[run_id]
    log_lines = st['log']

    def log(msg):
        log_lines.append(f"[{time.strftime('%H:%M:%S')}] {msg}")
        if len(log_lines) > 4000:
            del log_lines[:len(log_lines) - 4000]

    def stop_check():
        if st['stop']:
            raise RunAborted()

    lane = None
    threads = []
    status = 'finished'
    error = ''
    proto = proto_name(ip_version)
    try:
        # 目标地址取自本次测试记录（创建时就已按协议选定并写入），这里只做防御性校验
        run = db.get_run(run_id) or {}
        target_ip = (run.get('target_host') or '').strip()
        try:
            addr = ipaddress.ip_address(target_ip)
        except ValueError:
            raise RuntimeError(
                f'目标机地址无效（{target_ip!r}），请点击机器管理里的「探测地址」后重试')
        if addr.version != ip_version:
            raise RuntimeError(
                f'目标机地址 {target_ip} 与所选协议 {proto} 不一致，请重新选择后再试')
        cfg = run_cfg(run)
        kind = f"UDP（-b {cfg['udp_bandwidth']}/流）" if cfg['udp'] else 'TCP'
        log(f"=== 测试开始 | 目标机器: {target['name']} ({target_ip}) | 协议: {proto} | "
            f"{kind} | -P {cfg['streams']} -t {cfg['duration']} | "
            f"ping -c {cfg['ping_count']} ===")
        log('[目标] 通过 Agent 检查/安装 iperf3 与 ping ...')
        code, out = _job(target, aj.SCRIPT_ENSURE, 600, log, stop_check)
        if code != 0:
            raise RuntimeError('目标机 iperf3/ping 安装失败: ' + (out or '').strip()[-300:])

        items = db.get_run_items(run_id)
        # 目标机监听端口（NAT 后可单独指定）+ 各后端机单独指定的连接端口
        tport = target_port_of(cfg)
        ports = serve_ports(items, cfg)
        conn = sorted({item_port(it, cfg) for it in items}) or [cfg['port']]
        log(f"[目标] 启动 iperf3 server：监听端口 {'、'.join(str(p) for p in ports)}"
            f"；后端机连接端口 {'、'.join(str(p) for p in conn)}"
            + ('（目标机在 NAT 后，两者不同属正常）' if tport not in conn else ''))
        for p in ports:
            required = (p == tport)
            code, out = _job(target, aj.script_start_server(p), 30, log, stop_check)
            if code != 0:
                if required:
                    raise RuntimeError(f'目标机 iperf3 -s 启动失败（监听端口 {p}）: '
                                       + (out or '').strip()[-300:])
                # 后端机单独指定的连接端口：NAT/端口转发场景下本机不必监听（也可能起不来）
                log(f"[目标] ⚠️ 端口 {p} 未能在目标机本机监听（可能已被占用或本就走 NAT 映射）；"
                    f"若该端口是直连目标机的，对应后端机会失败")

        log(f"[目标] iperf3 -s 已就绪（监听 {tport}，{'UDP' if cfg['udp'] else 'TCP'}"
            f"{'，' + str(cfg['streams']) + ' 线程' if cfg['streams'] > 1 else ''}），"
            f"开始流水线测试：iperf3 串行 / ping 并行")

        lane = Iperf3Lane()
        for idx, it in enumerate(items):
            t = threading.Thread(target=_run_backend,
                                 args=(lane, st, log, stop_check, target, it, idx, target_ip,
                                       proto, cfg),
                                 daemon=True)
            threads.append(t)
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        log('=== 全部后端测试结束，正在关闭目标机 iperf3 server ===')
        try:
            _job(target, aj.SCRIPT_STOP_SERVER, 15, log, None)
        except Exception:
            pass
    except RunAborted:
        status = 'stopped'
        error = '用户手动停止测试'
        log('=== 测试被手动停止 ===')
        if lane:
            lane.stop()
        for t in threads:
            t.join(timeout=15)
        db.fail_open_items(run_id, '测试被手动停止')
    except Exception as e:
        status = 'failed'
        error = str(e) or e.__class__.__name__
        log(f'[致命错误] {error}')
        log(traceback.format_exc()[-800:])
        if lane:
            lane.stop()
        for t in threads:
            t.join(timeout=15)
        db.fail_open_items(run_id, '目标机异常，未执行')
    finally:
        if lane:
            lane.stop()
        for t in threads:
            t.join(timeout=15)

        run = db.get_run(run_id)
        items = db.get_run_items(run_id)
        db.cancel_queued_jobs([it['machine_id'] for it in items if it['machine_id']] +
                              [target['id']])
        total = len(items)
        done = sum(1 for i in items if i['status'] == 'done')
        if status == 'finished':
            if total and done == total:
                status = 'finished'
            elif done:
                status = 'partial'
            else:
                status = 'failed'
        target_info = {
            'name': run['target_name'], 'host': run['target_host'],
            'region': run['target_region'], 'bandwidth': run['target_bandwidth'],
        }
        report = quality.build_report(run, target_info, items)
        db.finish_run(run_id, status=status, report=report,
                      log_text='\n'.join(log_lines), error=error)
        log(f'=== 测试结束，状态: {status}，报告已生成 ===')
        with _state_lock:
            _active.pop(run_id, None)


def _run_backend(lane, st, log, stop_check, target, it, idx, target_ip, proto='IPv4', cfg=None):
    """单台后端的流水线：环境检查（并行）→ 等通道 → iperf3 上/下行（串行）→ ping（并行）。

    iperf3 / ping 都用目标地址字面量决定协议族（IPv4 或 IPv6），不再依赖本机默认路由；
    线程数 / 时长 / 端口 / TCP-UDP 均来自本次测试的参数（cfg）。
    """
    cfg = cfg or dict(DEFAULT_PARAMS)
    iid = it['id']
    name = it['machine_name']
    # 该后端机本次用的端口（逐台可不同），单独的 cfg 供命令拼装使用
    my_port = item_port(it, cfg)
    my_cfg = dict(cfg, port=my_port)
    db.update_item(iid, status='running', phase='检查环境')
    log(f"--- 后端 {name} ({it['machine_host']}) 开始 ---")
    lane_passed = False
    try:
        m = db.get_machine(it['machine_id']) if it['machine_id'] else None
        if not m:
            raise RuntimeError('机器已被删除，无法执行')
        if not db.machine_online(m):
            raise RuntimeError('该机器 Agent 离线，无法执行测试')

        # 环境检查与其他机器并行，互不干扰
        code, out = _job(m, aj.SCRIPT_ENSURE, 600, log, stop_check)
        if code != 0:
            raise RuntimeError('iperf3/ping 检查安装失败: ' + (out or '').strip()[-300:])

        tq = target_ip
        # 防御性校验：目标地址必须是合法 IP，避免任何路径上的命令注入
        try:
            ipaddress.ip_address(tq)
        except ValueError:
            raise RuntimeError(f'目标机地址无效（{tq!r}），请让 Agent 重新接入以更新 IP')

        # 等待 iperf3 通道（前一台的 iperf3 结束后立刻轮到本机，ping 不占通道）
        db.update_item(iid, phase='等待 iperf3 通道')
        if not lane.acquire(idx):
            raise RunAborted()
        lane_passed = True
        try:
            # 通道内先预检本机要用的端口；不通则尝试自动重启目标机该端口的 server
            code, out = _job(m, aj.script_check_port(tq, my_port), 15, log, stop_check)
            if code != 0:
                log(f"[{name}] {my_port} 端口不通，尝试重启目标机 iperf3 server ...")
                code2, out2 = _job(target, aj.script_start_server(my_port), 30, log, stop_check)
                if code2 != 0:
                    raise RuntimeError('目标机 iperf3 server 启动失败: ' + (out2 or '').strip()[-200:])
                code, out = _job(m, aj.script_check_port(tq, my_port), 15, log, stop_check)
                if code != 0:
                    hints = ''
                    if proto == 'IPv6':
                        hints += '；IPv6 测试需本机与目标机均具备 IPv6 连通性'
                    if cfg['udp']:
                        hints += f'；UDP 测试需同时放行 {my_port}/UDP'
                    if my_port != target_port_of(cfg):
                        hints += (f'；目标机本机监听的是 {target_port_of(cfg)}，'
                                  f'{my_port} 需由 NAT / 端口转发映射进来')
                    raise RuntimeError(
                        f"目标机 {tq} 的 {my_port} 端口从本机不可达"
                        f"（请检查目标机防火墙/安全组是否放行 {my_port}{hints}）")

            cmd_up = build_iperf_cmd(tq, my_cfg)
            db.update_item(iid, phase='上行测试')
            log(f'[{name}] $ {cmd_up}   （{proto}）')
            code, up_raw = _job(m, cmd_up, cfg['duration'] + 60, log, stop_check)
            if code != 0:
                raise RuntimeError('上行测试失败: ' + (up_raw or '').strip()[-200:])
            time.sleep(1)

            cmd_down = build_iperf_cmd(tq, my_cfg, reverse=True)
            db.update_item(iid, phase='下行测试')
            log(f'[{name}] $ {cmd_down}   （{proto}）')
            code, down_raw = _job(m, cmd_down, cfg['duration'] + 60, log, stop_check)
            if code != 0:
                raise RuntimeError('下行测试失败: ' + (down_raw or '').strip()[-200:])
        finally:
            lane.release()  # 无论如何立刻让出通道，下一台马上开始 iperf3
        time.sleep(1)

        # ping 不占通道，与后续机器的 iperf3 并行
        cmd_ping = f'ping -c {cfg["ping_count"]} -i 1 {shlex.quote(tq)}'
        db.update_item(iid, phase=f"ping {cfg['ping_count']}次（约{cfg['ping_count']}秒）")
        log(f"[{name}] $ {cmd_ping}  （约需 {cfg['ping_count']} 秒，与其它机器的 iperf3 并行）")
        code, ping_raw = _job(m, cmd_ping, cfg['ping_count'] + 120, log, stop_check)
        if 'packets transmitted' not in (ping_raw or ''):
            raise RuntimeError('ping 失败: ' + (ping_raw or '').strip()[-200:])

        metrics = quality.parse_metrics(ping_raw, up_raw, down_raw)
        quality.evaluate(metrics, it)
        db.update_item(iid, status='done', phase='完成', ping_raw=ping_raw, up_raw=up_raw,
                       down_raw=down_raw,
                       metrics=json.dumps(metrics, ensure_ascii=False))
        extra = ''
        if metrics.get('udp'):
            extra = (f"| UDP 丢包 {quality.fmt_num(metrics.get('up_udp_loss_pct'), 2)}%/"
                     f"{quality.fmt_num(metrics.get('down_udp_loss_pct'), 2)}% ")
        log(f"[{name}] ✅ 完成 | 上行 {quality.fmt_num(metrics.get('up_mbits'))} Mbit/s "
            f"| 下行 {quality.fmt_num(metrics.get('down_mbits'))} Mbit/s "
            f"| 丢包 {quality.fmt_num(metrics.get('loss_pct'))}% " + extra
            + f"| 评价: {metrics.get('rating')}")
    except RunAborted:
        db.update_item(iid, status='failed', error='手动停止')
        log(f'[{name}] ⏹ 已停止')
    except Exception as e:
        db.update_item(iid, status='failed', error=str(e))
        log(f'[{name}] ❌ 失败: {e}')
    finally:
        if not lane_passed:
            # 失败/中止也要把轮次让出去，避免阻塞后面的机器
            if lane.acquire(idx):
                lane.release()


# ---------------- 地址探测（后台补齐机器的 IPv4 / IPv6） ----------------

DISCOVER_COOLDOWN = 600   # 同一台机器两次自动探测的最小间隔（秒）
_discover_next = {}       # machine_id -> 下次允许自动探测的时间戳


def start_discovery_worker():
    """启动后台线程：为地址不全的在线机器补一次本机地址探测。

    走 Agent 任务队列（Agent 无需升级）：脚本只在本机查地址，不访问外网。
    面板只监听 IPv4 时也能拿到机器的 IPv6，从而支持 IPv6 测试。
    """
    threading.Thread(target=_discovery_loop, daemon=True).start()


def _discovery_loop():
    while True:
        try:
            _discovery_pass()
        except Exception:
            pass
        time.sleep(5)


def _discovery_pass():
    if active_run_id():
        return                      # 测试进行中不打扰
    now = time.time()
    for m in db.get_machines():
        if m['ip4'] and m['ip6']:
            continue                # 两族地址都已知
        if not db.machine_online(m):
            continue
        if now < _discover_next.get(m['id'], 0):
            continue
        _discover_next[m['id']] = now + DISCOVER_COOLDOWN
        queue_discovery(m['id'])


def queue_discovery(mid, force=False):
    """异步派发一次地址探测（不阻塞请求线程）。force=True 时覆盖已存地址。"""
    def run():
        try:
            found = discover_machine(mid, force=force)
            print(f'[探测] 机器 #{mid} 地址: {found or "未获取到全局地址"}', flush=True)
        except Exception as e:
            reason = _probe_fail_reason(e)
            try:
                db.set_machine_probe(mid, reason)
            except Exception:
                pass
            print(f'[探测] 机器 #{mid} 失败: {reason}', flush=True)
    threading.Thread(target=run, daemon=True).start()


def _probe_fail_reason(e):
    """把探测失败转成面板上能看懂、能照着排查的一句话。"""
    msg = str(e)
    if '超时' in msg or '未领取' in msg:
        return ('地址探测任务没有回传结果：Agent 可能没在跑任务或拒收了任务，'
                '到机器上执行 tail -5 /var/lib/iperf3-fleet/agent.log 查看原因')
    return msg[:200]


def discover_machine(mid, force=False):
    """派发地址探测任务并解析结果。force=True 时用探测值覆盖已存地址（手动刷新）。"""
    m = db.get_machine(mid)
    if not m:
        raise RuntimeError('机器不存在')
    if not db.machine_online(m):
        raise RuntimeError(f'机器「{m["name"]}」的 Agent 未上线，无法探测地址')
    code, out = _job(m, aj.SCRIPT_REPORT_IPS, 20, lambda _s: None, None)
    found = aj.parse_ip_report(out)
    if found:
        db.set_machine_ips(mid, found, force=force)
        db.set_machine_probe(mid, '')
    else:
        db.set_machine_probe(mid, '这台机器没有全局可路由地址（只有私网 / ULA / 链路本地地址）')
    return found
