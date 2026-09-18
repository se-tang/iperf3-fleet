"""测试编排 v2：通过 Agent 任务队列远程执行（面板不再直连机器）。

流水线：iperf3 上/下行全局串行（多台同时测速会互相抢带宽，结果作废），
ping 200 次不占测速通道，与后续机器的 iperf3 并行执行，充分利用等待时间。
总时长 ≈ 环境准备 + 台数×25 秒 + 最后 200 秒。
"""
import json
import threading
import time
import traceback

from . import agent_jobs as aj
from . import db, quality

_state_lock = threading.Lock()
_active = {}  # run_id -> {'stop': bool, 'log': [..]}


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


def start_run(target_id, backend_ids):
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
        for bid in backend_ids:
            m = db.get_machine(bid)
            if not m:
                raise RuntimeError(f'后端机器 #{bid} 不存在')
            if m['role'] != 'backend':
                raise RuntimeError(f'机器「{m["name"]}」的角色不是「后端机器」，不能作为后端参加测试')
        run_id = db.create_run(target, backend_ids)
        _active[run_id] = {'stop': False, 'log': []}
    threading.Thread(target=_worker, args=(run_id, target), daemon=True).start()
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


def _worker(run_id, target):
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
    try:
        log(f"=== 测试开始 | 目标机器: {target['name']} ({target['agent_ip'] or 'IP待agent上报'}) ===")
        log('[目标] 通过 Agent 检查/安装 iperf3 与 ping ...')
        code, out = _job(target, aj.SCRIPT_ENSURE, 600, log, stop_check)
        if code != 0:
            raise RuntimeError('目标机 iperf3/ping 安装失败: ' + (out or '').strip()[-300:])

        code, out = _job(target, aj.SCRIPT_START_SERVER, 30, log, stop_check)
        if code != 0:
            raise RuntimeError('目标机 iperf3 -s 启动失败: ' + (out or '').strip()[-300:])
        log('[目标] iperf3 -s 已就绪（端口 5201），开始流水线测试：iperf3 串行 / ping 并行')

        items = db.get_run_items(run_id)
        lane = Iperf3Lane()
        for idx, it in enumerate(items):
            t = threading.Thread(target=_run_backend,
                                 args=(lane, st, log, stop_check, target, it, idx),
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


def _run_backend(lane, st, log, stop_check, target, it, idx):
    """单台后端的流水线：环境检查（并行）→ 等通道 → iperf3 上/下行（串行）→ ping（并行）。"""
    iid = it['id']
    name = it['machine_name']
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

        tq = target['agent_ip']

        # 等待 iperf3 通道（前一台的 iperf3 结束后立刻轮到本机，ping 不占通道）
        db.update_item(iid, phase='等待 iperf3 通道')
        if not lane.acquire(idx):
            raise RunAborted()
        lane_passed = True
        try:
            # 通道内先预检 5201；不通则尝试自动重启目标机 server（串行内重启，无竞争）
            code, out = _job(m, aj.script_check_port(tq), 15, log, stop_check)
            if code != 0:
                log(f'[{name}] 5201 不通，尝试重启目标机 iperf3 server ...')
                code2, out2 = _job(target, aj.SCRIPT_START_SERVER, 30, log, stop_check)
                if code2 != 0:
                    raise RuntimeError('目标机 iperf3 server 启动失败: ' + (out2 or '').strip()[-200:])
                code, out = _job(m, aj.script_check_port(tq), 15, log, stop_check)
                if code != 0:
                    raise RuntimeError(f'目标机 {tq} 的 5201/TCP 从本机不可达（请检查目标机防火墙是否放行 5201）')

            cmd_up = f'iperf3 -c {tq} -t 10'
            db.update_item(iid, phase='上行测试')
            log(f'[{name}] $ {cmd_up}')
            code, up_raw = _job(m, cmd_up, 90, log, stop_check)
            if code != 0:
                raise RuntimeError('上行测试失败: ' + (up_raw or '').strip()[-200:])
            time.sleep(1)

            cmd_down = f'iperf3 -c {tq} -R -t 10'
            db.update_item(iid, phase='下行测试')
            log(f'[{name}] $ {cmd_down}')
            code, down_raw = _job(m, cmd_down, 90, log, stop_check)
            if code != 0:
                raise RuntimeError('下行测试失败: ' + (down_raw or '').strip()[-200:])
        finally:
            lane.release()  # 无论如何立刻让出通道，下一台马上开始 iperf3
        time.sleep(1)

        # ping 不占通道，与后续机器的 iperf3 并行
        cmd_ping = f'ping -c 200 -i 1 {tq}'
        db.update_item(iid, phase='ping 200次（约3分钟）')
        log(f'[{name}] $ {cmd_ping}  （约需 200 秒，与其它机器的 iperf3 并行）')
        code, ping_raw = _job(m, cmd_ping, 300, log, stop_check)
        if 'packets transmitted' not in (ping_raw or ''):
            raise RuntimeError('ping 失败: ' + (ping_raw or '').strip()[-200:])

        metrics = quality.parse_metrics(ping_raw, up_raw, down_raw)
        quality.evaluate(metrics, it)
        db.update_item(iid, status='done', phase='完成', ping_raw=ping_raw, up_raw=up_raw,
                       down_raw=down_raw,
                       metrics=json.dumps(metrics, ensure_ascii=False))
        log(f"[{name}] ✅ 完成 | 上行 {quality.fmt_num(metrics.get('up_mbits'))} Mbit/s "
            f"| 下行 {quality.fmt_num(metrics.get('down_mbits'))} Mbit/s "
            f"| 丢包 {quality.fmt_num(metrics.get('loss_pct'))}% "
            f"| 评价: {metrics.get('rating')}")
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
