"""测试编排 v2：通过 Agent 任务队列远程执行（面板不再直连机器）。

流程：目标机 agent 启动 iperf3 -s → 后端逐台串行执行 上/下行 iperf3 + ping，
agent 心跳领取任务、流式回传输出，面板轮询任务状态汇总报告。
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
            if not db.get_machine(bid):
                raise RuntimeError(f'后端机器 #{bid} 不存在')
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
        log('[目标] iperf3 -s 已就绪，等待后端机器连接 (端口 5201)')

        items = db.get_run_items(run_id)
        for it in items:
            stop_check()
            _run_backend(st, log, stop_check, target, it)

        log('=== 全部后端测试结束，正在关闭目标机 iperf3 server ===')
        try:
            _job(target, aj.SCRIPT_STOP_SERVER, 15, log, None)
        except Exception:
            pass
    except RunAborted:
        status = 'stopped'
        error = '用户手动停止测试'
        log('=== 测试被手动停止 ===')
        db.fail_open_items(run_id, '测试被手动停止')
    except Exception as e:
        status = 'failed'
        error = str(e) or e.__class__.__name__
        log(f'[致命错误] {error}')
        log(traceback.format_exc()[-800:])
        db.fail_open_items(run_id, '目标机异常，未执行')
    finally:
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


def _run_backend(st, log, stop_check, target, it):
    iid = it['id']
    name = it['machine_name']
    db.update_item(iid, status='running')
    log(f"--- 后端 {name} ({it['machine_host']}) 开始 ---")
    try:
        m = db.get_machine(it['machine_id']) if it['machine_id'] else None
        if not m:
            raise RuntimeError('机器已被删除，无法执行')
        if not db.machine_online(m):
            raise RuntimeError('该机器 Agent 离线，无法执行测试')

        code, out = _job(m, aj.SCRIPT_ENSURE, 600, log, stop_check)
        if code != 0:
            raise RuntimeError('iperf3/ping 检查安装失败: ' + (out or '').strip()[-300:])

        # 目标机 server 掉线（如上一轮异常）则自动重启
        code, out = _job(target, aj.SCRIPT_ENSURE_SERVER, 30, log, stop_check)
        if code != 0:
            raise RuntimeError(f'目标机 iperf3 server 不可用: {(out or "").strip()[-200:]}')

        tq = target['agent_ip']

        cmd_up = f'iperf3 -c {tq} -t 10'
        log(f'[{name}] $ {cmd_up}')
        code, up_raw = _job(m, cmd_up, 90, log, stop_check)
        if code != 0:
            raise RuntimeError('上行测试失败: ' + (up_raw or '').strip()[-200:])
        time.sleep(1)

        cmd_down = f'iperf3 -c {tq} -R -t 10'
        log(f'[{name}] $ {cmd_down}')
        code, down_raw = _job(m, cmd_down, 90, log, stop_check)
        if code != 0:
            raise RuntimeError('下行测试失败: ' + (down_raw or '').strip()[-200:])
        time.sleep(1)

        cmd_ping = f'ping -c 200 -i 1 {tq}'
        log(f'[{name}] $ {cmd_ping}  （约需 200 秒）')
        code, ping_raw = _job(m, cmd_ping, 300, log, stop_check)
        if 'packets transmitted' not in (ping_raw or ''):
            raise RuntimeError('ping 失败: ' + (ping_raw or '').strip()[-200:])

        metrics = quality.parse_metrics(ping_raw, up_raw, down_raw)
        quality.evaluate(metrics, it)
        db.update_item(iid, status='done', ping_raw=ping_raw, up_raw=up_raw,
                       down_raw=down_raw,
                       metrics=json.dumps(metrics, ensure_ascii=False))
        log(f"[{name}] ✅ 完成 | 上行 {quality.fmt_num(metrics.get('up_mbits'))} Mbit/s "
            f"| 下行 {quality.fmt_num(metrics.get('down_mbits'))} Mbit/s "
            f"| 丢包 {quality.fmt_num(metrics.get('loss_pct'))}% "
            f"| 评价: {metrics.get('rating')}")
    except RunAborted:
        db.update_item(iid, status='failed', error='手动停止')
        raise
    except Exception as e:
        db.update_item(iid, status='failed', error=str(e))
        log(f'[{name}] ❌ 失败: {e}')
